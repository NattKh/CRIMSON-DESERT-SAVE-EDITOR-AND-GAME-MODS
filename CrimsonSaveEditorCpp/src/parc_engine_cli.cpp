/**
 * parc_engine_cli — CLI tool for PARC save manipulation.
 *
 * Usage:
 *   parc_engine_cli info <save_path>
 *   parc_engine_cli insert <save_path> <block_class> <list_field> <template.bin> [-o output.save]
 *   parc_engine_cli remove <save_path> <block_class> <list_field> <index> [-o output.save]
 *   parc_engine_cli roundtrip <save_path> <output.save>
 */
#include "parc_engine.h"
#include "parc_serializer.h"
#include "parc_xml.h"
#include "item_factory.h"
#include <cstdio>
#include <cstring>
#include <fstream>
#include <filesystem>
#include <map>
#include <sstream>
#include <unordered_map>
#include <nlohmann/json.hpp>
#include <chrono>
#include <set>
#include <string>

using namespace ParcEngine;

static std::vector<uint8_t> ReadBinaryFile(const char* path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { fprintf(stderr, "Cannot open: %s\n", path); exit(1); }
    f.seekg(0, std::ios::end);
    size_t sz = f.tellg();
    f.seekg(0);
    std::vector<uint8_t> data(sz);
    f.read(reinterpret_cast<char*>(data.data()), sz);
    return data;
}

static std::vector<uint8_t> HexToBytes(const char* hex) {
    std::vector<uint8_t> bytes;
    size_t len = strlen(hex);
    for (size_t i = 0; i + 1 < len; i += 2) {
        char buf[3] = {hex[i], hex[i+1], 0};
        bytes.push_back((uint8_t)strtoul(buf, nullptr, 16));
    }
    return bytes;
}

int main(int argc, char* argv[]) {
    if (argc < 3) {
        fprintf(stderr,
            "Usage:\n"
            "  parc_engine_cli info <save_path>\n"
            "  parc_engine_cli insert <save_path> <block> <field> <template.bin|hex:AABB...> [-o out.save]\n"
            "  parc_engine_cli remove <save_path> <block> <field> <index> [-o out.save]\n"
            "  parc_engine_cli roundtrip <save_path> <output.save>\n"
        );
        return 1;
    }

    const char* cmd = argv[1];
    const char* save_path = argv[2];

    auto t0 = std::chrono::high_resolution_clock::now();

    // ── EXPORT-XML ──
    // export-xml <save> <out.xml> [--node "Block.field[i]..."]
    if (strcmp(cmd, "export-xml") == 0) {
        if (argc < 4) { fprintf(stderr, "Usage: export-xml <save> <out.xml> [--node path]\n"); return 1; }
        const char* out = argv[3];
        std::string node_path;
        for (int i = 4; i < argc; i++) {
            if (strcmp(argv[i], "--node") == 0 && i + 1 < argc) node_path = argv[i + 1];
        }
        auto tree = LoadSave(save_path);
        auto err = ParcXml::ExportXml(tree, out, node_path);
        if (!err.empty()) { fprintf(stderr, "EXPORT FAILED: %s\n", err.c_str()); return 1; }
        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Exported %s%s -> %s (%.1f ms)\n", save_path,
            node_path.empty() ? "" : (" [" + node_path + "]").c_str(), out,
            std::chrono::duration<double, std::milli>(t1 - t0).count());
        return 0;
    }

    // ── IMPORT-XML ──
    // import-xml <whole-save.xml> -o <out.save>     (save_path arg = the xml)
    if (strcmp(cmd, "import-xml") == 0) {
        const char* out = nullptr;
        for (int i = 3; i < argc; i++) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) out = argv[i + 1];
        }
        if (!out) { fprintf(stderr, "Usage: import-xml <save.xml> -o <out.save>\n"); return 1; }

        std::vector<uint8_t> blob, header;
        auto err = ParcXml::ImportXml(save_path, blob, header);
        if (!err.empty()) { fprintf(stderr, "IMPORT FAILED: %s\n", err.c_str()); return 1; }
        fprintf(stderr, "Imported: %zu byte blob, header=%zu bytes, self-check passed\n",
            blob.size(), header.size());

        SaveTree tree;
        tree.blob = std::move(blob);
        tree.is_encrypted = !header.empty();
        tree.original_header = std::move(header);
        WriteSave(tree, out);
        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Written: %s (%.1f ms total)\n", out,
            std::chrono::duration<double, std::milli>(t1 - t0).count());
        return 0;
    }

    // ── MAKE-LOBBY ──
    // make-lobby <out_lobby.save> [--name "Slot Name"] [--charkey N] [--level N]
    if (strcmp(cmd, "make-lobby") == 0) {
        const char* out = argv[2];
        std::string name = "Custom Save";
        int charkey = 1, level = 1;
        for (int i = 3; i < argc; i++) {
            if (strcmp(argv[i], "--name") == 0 && i + 1 < argc) name = argv[i + 1];
            if (strcmp(argv[i], "--charkey") == 0 && i + 1 < argc) charkey = atoi(argv[i + 1]);
            if (strcmp(argv[i], "--level") == 0 && i + 1 < argc) level = atoi(argv[i + 1]);
        }
        std::vector<uint8_t> blob, header;
        auto err = ParcXml::BuildLobbySave(name, blob, header, charkey, level);
        if (!err.empty()) { fprintf(stderr, "MAKE-LOBBY FAILED: %s\n", err.c_str()); return 1; }
        SaveTree tree;
        tree.blob = std::move(blob);
        tree.is_encrypted = true;
        tree.original_header = std::move(header);
        WriteSave(tree, out);
        fprintf(stderr, "Lobby written: %s (name=\"%s\" charkey=%d level=%d)\n",
            out, name.c_str(), charkey, level);
        return 0;
    }

    // ── IMPORT-NODE ──
    // import-node <save> <node.xml> [--path "Block.field[i]"] -o <out.save>
    if (strcmp(cmd, "import-node") == 0) {
        if (argc < 4) {
            fprintf(stderr, "Usage: import-node <save> <node.xml> [--path p] -o <out.save>\n");
            return 1;
        }
        const char* xml = argv[3];
        const char* out = nullptr;
        std::string path_override;
        for (int i = 4; i < argc; i++) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) out = argv[i + 1];
            if (strcmp(argv[i], "--path") == 0 && i + 1 < argc) path_override = argv[i + 1];
        }
        if (!out) { fprintf(stderr, "import-node requires -o <out.save>\n"); return 1; }

        auto tree = LoadSave(save_path);
        size_t old_size = tree.blob.size();
        auto err = ParcXml::ImportNodeXml(tree, xml, path_override);
        if (!err.empty()) { fprintf(stderr, "IMPORT FAILED: %s\n", err.c_str()); return 1; }
        fprintf(stderr, "Grafted node: blob %zu -> %zu bytes (delta=%d)\n",
            old_size, tree.blob.size(), (int)tree.blob.size() - (int)old_size);

        // Self-check: reparse + reserialize must be stable
        auto re = ParcSerializer::Serialize(tree.parsed, tree.blob);
        if (re != tree.blob) {
            fprintf(stderr, "SELF-CHECK FAILED: grafted blob unstable — refusing to write\n");
            return 1;
        }
        fprintf(stderr, "Self-check passed (reserialize stable)\n");
        WriteSave(tree, out);
        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Written: %s (%.1f ms total)\n", out,
            std::chrono::duration<double, std::milli>(t1 - t0).count());
        return 0;
    }

    // ── INFO ──
    if (strcmp(cmd, "info") == 0) {
        auto tree = LoadSave(save_path);
        auto t1 = std::chrono::high_resolution_clock::now();
        double ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

        fprintf(stderr, "Loaded: %s\n", save_path);
        fprintf(stderr, "  Encrypted: %s\n", tree.is_encrypted ? "yes" : "no");
        fprintf(stderr, "  Blob size: %zu bytes\n", tree.blob.size());
        fprintf(stderr, "  Schema: %zu types\n", tree.parsed.schema.types.size());
        fprintf(stderr, "  TOC: %zu entries\n", tree.parsed.toc.entries.size());
        fprintf(stderr, "  Objects: %zu\n", tree.parsed.objects.size());
        fprintf(stderr, "  PO table: %zu entries\n", tree.po_table.size());
        fprintf(stderr, "  Trailing sizes: %zu entries\n", tree.ts_table.size());
        fprintf(stderr, "  Load time: %.1f ms\n", ms);

        // Print object summary
        for (auto& obj : tree.parsed.objects) {
            int list_count = 0;
            for (auto& f : obj.fields) {
                list_count += (int)f.list_elements.size();
            }
            if (list_count > 0) {
                fprintf(stderr, "  [%u] %s: %d list elements\n",
                    obj.entry_index, obj.class_name.c_str(), list_count);
            }
        }
        return 0;
    }

    // ── INSPECT ──
    if (strcmp(cmd, "inspect") == 0) {
        auto tree = LoadSave(save_path);
        const char* target = (argc > 3) ? argv[3] : "InventorySaveData";
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name.find(target) == std::string::npos) continue;
            fprintf(stderr, "Block [%u] %s: %zu fields\n", obj.entry_index, obj.class_name.c_str(), obj.fields.size());
            for (size_t fi = 0; fi < obj.fields.size(); ++fi) {
                auto& f = obj.fields[fi];
                fprintf(stderr, "  field[%zu] %s k=%u present=%d rv=%zu elems=%zu\n",
                    fi, f.name.c_str(), f.meta_kind, f.present?1:0,
                    f.raw_value.size(), f.list_elements.size());
                // Show nested lists inside first element's child_fields
            if (f.list_elements.size() > 0) {
                auto& e0 = f.list_elements[0];
                for (size_t ci = 0; ci < e0.child_fields.size(); ++ci) {
                    auto& cf = e0.child_fields[ci];
                    if ((cf.meta_kind == 6 || cf.meta_kind == 7) && cf.list_elements.size() > 0) {
                        fprintf(stderr, "    elem[0].%s has %zu nested elems\n",
                            cf.name.c_str(), cf.list_elements.size());
                        auto& ne = cf.list_elements[0];
                        fprintf(stderr, "      nested[0] type=%s cf=%zu rv=%zu\n",
                            ne.child_type_name.c_str(), ne.child_fields.size(), ne.raw_value.size());
                        for (size_t ni = 0; ni < ne.child_fields.size() && ni < 25; ++ni) {
                            auto& nf = ne.child_fields[ni];
                            fprintf(stderr, "        nf[%zu] %s k=%u p=%d rv=%zu off=[0x%X..0x%X]\n",
                                ni, nf.name.c_str(), nf.meta_kind, nf.present?1:0,
                                nf.raw_value.size(), nf.start_offset, nf.end_offset);
                        }
                    }
                }
            }
            if (f.list_elements.size() > 0 && f.list_elements.size() <= 3) {
                    for (size_t ei = 0; ei < f.list_elements.size(); ++ei) {
                        auto& e = f.list_elements[ei];
                        fprintf(stderr, "    elem[%zu] type=%s cf=%zu rv=%zu\n",
                            ei, e.child_type_name.c_str(), e.child_fields.size(), e.raw_value.size());
                        for (size_t ci = 0; ci < e.child_fields.size() && ci < 20; ++ci) {
                            auto& cf = e.child_fields[ci];
                            fprintf(stderr, "      cf[%zu] %s k=%u p=%d rv=%zu off=[0x%X..0x%X]\n",
                                ci, cf.name.c_str(), cf.meta_kind, cf.present?1:0,
                                cf.raw_value.size(), cf.start_offset, cf.end_offset);
                        }
                    }
                } else if (f.list_elements.size() > 3) {
                    auto& e = f.list_elements[0];
                    fprintf(stderr, "    elem[0] type=%s cf=%zu rv=%zu\n",
                        e.child_type_name.c_str(), e.child_fields.size(), e.raw_value.size());
                    for (size_t ci = 0; ci < e.child_fields.size() && ci < 20; ++ci) {
                        auto& cf = e.child_fields[ci];
                        fprintf(stderr, "      cf[%zu] %s k=%u p=%d rv=%zu\n",
                            ci, cf.name.c_str(), cf.meta_kind, cf.present?1:0, cf.raw_value.size());
                    }
                    fprintf(stderr, "    ... %zu more elements\n", f.list_elements.size() - 1);
                }
            }
            break;
        }
        return 0;
    }

    // ── SCHEMA ──
    if (strcmp(cmd, "schema") == 0) {
        auto tree = LoadSave(save_path);
        for (size_t i = 0; i < tree.parsed.schema.types.size(); ++i) {
            fprintf(stderr, "  [%zu] %s\n", i, tree.parsed.schema.types[i].name.c_str());
        }
        return 0;
    }

    // ── VALIDATE ──
    if (strcmp(cmd, "validate") == 0) {
        auto tree = LoadSave(save_path);
        auto& blob = tree.blob;
        int errors = 0;
        int po_count = 0;
        static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

        // Check TOC entries
        for (size_t i = 0; i < tree.parsed.toc.entries.size(); ++i) {
            auto& te = tree.parsed.toc.entries[i];
            if (te.data_offset + te.data_size > blob.size()) {
                fprintf(stderr, "ERROR: TOC[%zu] block extends past blob end (off=0x%X size=%u blob=%zu)\n",
                    i, te.data_offset, te.data_size, blob.size());
                errors++;
            }
            if (te.class_index >= tree.parsed.schema.types.size()) {
                fprintf(stderr, "ERROR: TOC[%zu] class_index %u >= type count %zu\n",
                    i, te.class_index, tree.parsed.schema.types.size());
                errors++;
            }
        }

        // Scan ALL sentinels in blob, verify real POs are self-referential
        int false_positives = 0;
        for (size_t p = tree.parsed.schema.schema_end; p + 12 <= blob.size(); ++p) {
            if (memcmp(blob.data() + p, SENT, 8) == 0) {
                uint32_t po_pos = (uint32_t)(p + 8);
                uint32_t po_val = 0;
                memcpy(&po_val, blob.data() + po_pos, 4);
                uint32_t expected = po_pos + 4;

                // Skip TOC sentinels
                uint32_t toc_start = tree.parsed.schema.schema_end + 12;
                uint32_t toc_end = toc_start + (uint32_t)tree.parsed.toc.entries.size() * 20;
                if (p >= toc_start && p < toc_end) continue;

                if (po_val == expected) {
                    po_count++; // valid self-referential PO
                } else {
                    false_positives++; // data bytes forming 0xFF×8, not a real PO
                }
            }
        }

        // Check trailing sizes: after each PO, the payload starts, and at the end
        // there should be a u32 = distance from payload_start to that u32.
        // (This is harder to validate without full parsing, so we check POs only)

        // Check stream_size matches blob size
        uint32_t stream_size = tree.parsed.toc.stream_size;
        if (stream_size != blob.size()) {
            fprintf(stderr, "ERROR: stream_size=%u but blob=%zu\n", stream_size, blob.size());
            errors++;
        }

        // Check type indices only at REAL sentinels (where PO is valid)
        int bad_ti = 0;
        for (size_t p = tree.parsed.schema.schema_end; p + 12 <= blob.size(); ++p) {
            if (memcmp(blob.data() + p, SENT, 8) == 0) {
                uint32_t toc_start = tree.parsed.schema.schema_end + 12;
                uint32_t toc_end = toc_start + (uint32_t)tree.parsed.toc.entries.size() * 20;
                if (p >= toc_start && p < toc_end) continue;

                uint32_t po_pos = (uint32_t)(p + 8);
                uint32_t po_val = 0;
                memcpy(&po_val, blob.data() + po_pos, 4);
                if (po_val != po_pos + 4) continue; // not a real sentinel

                // Real sentinel — check type_index 3 bytes before
                if (p >= 3) {
                    uint16_t ti = 0;
                    memcpy(&ti, blob.data() + p - 3, 2);
                    if (ti >= tree.parsed.schema.types.size()) {
                        if (bad_ti < 20) {
                            fprintf(stderr, "ERROR: type_index %u at real sentinel 0x%zX >= type count %zu\n",
                                ti, p, tree.parsed.schema.types.size());
                        }
                        bad_ti++;
                        errors++;
                    }
                }
            }
        }
        if (bad_ti > 20) fprintf(stderr, "  ... and %d more bad type_index errors\n", bad_ti - 20);

        fprintf(stderr, "\nValidation: %d valid POs, %d false-positive sentinels, %d errors\n",
            po_count, false_positives, errors);
        if (errors == 0) fprintf(stderr, "PASS — PARC structure is valid\n");
        return errors > 0 ? 1 : 0;
    }

    // ── ROUNDTRIP ──
    if (strcmp(cmd, "roundtrip") == 0) {
        if (argc < 4) { fprintf(stderr, "Usage: roundtrip <save> <output>\n"); return 1; }
        const char* output = argv[3];

        auto tree = LoadSave(save_path);
        WriteSave(tree, output);

        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Roundtrip: %s -> %s (%.1f ms)\n", save_path, output,
            std::chrono::duration<double, std::milli>(t1 - t0).count());
        return 0;
    }

    // ── DUMP-BLOB — write raw decrypted PARC blob to file ──
    if (strcmp(cmd, "dump-blob") == 0) {
        if (argc < 4) { fprintf(stderr, "Usage: dump-blob <save> <output.bin>\n"); return 1; }
        auto tree = LoadSave(save_path);
        std::ofstream f(argv[3], std::ios::binary);
        f.write(reinterpret_cast<const char*>(tree.blob.data()), tree.blob.size());
        fprintf(stderr, "Dumped %zu bytes to %s\n", tree.blob.size(), argv[3]);
        return 0;
    }

    // ── COMPARE — block-by-block comparison of two saves ──
    if (strcmp(cmd, "compare") == 0) {
        if (argc < 4) { fprintf(stderr, "Usage: compare <save_a> <save_b>\n"); return 1; }
        auto a = LoadSave(save_path);
        auto b = LoadSave(argv[3]);
        fprintf(stderr, "A: %zu bytes, %zu objects\n", a.blob.size(), a.parsed.objects.size());
        fprintf(stderr, "B: %zu bytes, %zu objects\n", b.blob.size(), b.parsed.objects.size());

        // Build block map for B
        std::unordered_map<std::string, const SaveParserCpp::ObjectBlock*> b_blocks;
        for (auto& obj : b.parsed.objects) b_blocks[obj.class_name + ":" + std::to_string(obj.entry_index)] = &obj;

        int same = 0, differ = 0, a_only = 0;
        for (auto& oa : a.parsed.objects) {
            std::string key = oa.class_name + ":" + std::to_string(oa.entry_index);
            auto it = b_blocks.find(key);
            if (it == b_blocks.end()) { a_only++; continue; }
            auto& ob = *it->second;

            if (oa.data_size != ob.data_size) {
                fprintf(stderr, "DIFFER [%u] %s: size %u vs %u (delta=%d)\n",
                    oa.entry_index, oa.class_name.c_str(), oa.data_size, ob.data_size,
                    (int)ob.data_size - (int)oa.data_size);
                differ++;
                continue;
            }

            // Same size — byte compare the data content (excluding POs which differ by position)
            int byte_diffs = 0;
            uint32_t first_diff_off = 0;
            for (uint32_t i = 0; i < oa.data_size; i++) {
                uint32_t pa = oa.data_offset + i;
                uint32_t pb = ob.data_offset + i;
                if (pa < a.blob.size() && pb < b.blob.size()) {
                    if (a.blob[pa] != b.blob[pb]) {
                        // Skip PO diffs (4 bytes after 8xFF sentinel)
                        bool is_po = false;
                        if (i >= 8) {
                            bool sent = true;
                            for (int j = 0; j < 8; j++) {
                                if (a.blob[oa.data_offset + i - 8 + j] != 0xFF) { sent = false; break; }
                            }
                            if (sent && i % 1 == 0) is_po = true; // near sentinel
                        }
                        if (!is_po) byte_diffs++;
                        if (byte_diffs == 1) first_diff_off = i;
                    }
                }
            }
            if (byte_diffs > 0) {
                fprintf(stderr, "DIFFER [%u] %s: %d non-PO byte diffs (first at +0x%X)\n",
                    oa.entry_index, oa.class_name.c_str(), byte_diffs, first_diff_off);
                differ++;
            } else {
                same++;
            }
        }
        fprintf(stderr, "\nSummary: %d identical, %d differ, %d in A only\n", same, differ, a_only);
        return 0;
    }

    // ── BLOCK-TRANSPLANT — blob-splice approach (same as GUI SaveRepair::TransplantBlock) ──
    if (strcmp(cmd, "block-transplant") == 0) {
        if (argc < 5) {
            fprintf(stderr, "Usage: block-transplant <target_save> <donor_save> <block1>[,block2,...] [-o out.save]\n"
                "  Replaces entire top-level blocks via blob splice + PO rebase.\n"
                "  Example: block-transplant slot100.save slot108.save MercenaryClanSaveData,FriendlySaveData -o out.save\n");
            return 1;
        }
        const char* donor_path = argv[3];
        const char* block_arg = argv[4];
        const char* output = save_path;
        for (int i = 5; i < argc; ++i) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[++i];
        }

        // Parse comma-separated block names
        std::vector<std::string> block_names;
        {
            std::string s = block_arg;
            size_t pos = 0;
            while ((pos = s.find(',')) != std::string::npos) {
                block_names.push_back(s.substr(0, pos));
                s = s.substr(pos + 1);
            }
            if (!s.empty()) block_names.push_back(s);
        }

        fprintf(stderr, "Loading target: %s\n", save_path);
        auto target = LoadSave(save_path);
        fprintf(stderr, "  blob=%zu types=%zu objects=%zu\n",
            target.blob.size(), target.parsed.schema.types.size(), target.parsed.objects.size());

        fprintf(stderr, "Loading donor: %s\n", donor_path);
        auto donor = LoadSave(donor_path);
        fprintf(stderr, "  blob=%zu types=%zu objects=%zu\n",
            donor.blob.size(), donor.parsed.schema.types.size(), donor.parsed.objects.size());

        // Schema merge
        std::unordered_map<uint16_t, uint16_t> remap;
        int schema_added = 0;
        for (size_t di = 0; di < donor.parsed.schema.types.size(); di++) {
            const auto& dt = donor.parsed.schema.types[di];
            int found = -1;
            for (size_t ti = 0; ti < target.parsed.schema.types.size(); ti++) {
                if (target.parsed.schema.types[ti].name == dt.name) { found = (int)ti; break; }
            }
            if (found < 0) {
                found = (int)target.parsed.schema.types.size();
                auto copy = dt; copy.index = (uint32_t)found;
                target.parsed.schema.types.push_back(std::move(copy));
                schema_added++;
            }
            remap[(uint16_t)di] = (uint16_t)found;
        }
        target.parsed.schema.type_count = (uint16_t)target.parsed.schema.types.size();
        // Refresh name map
        target.name_to_type_idx.clear();
        for (size_t i = 0; i < target.parsed.schema.types.size(); i++)
            target.name_to_type_idx[target.parsed.schema.types[i].name] = (uint32_t)i;
        fprintf(stderr, "Schema: %d types added\n", schema_added);

        // Sentinel scanner for PO collection
        static const uint8_t SENT8[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
        auto collectPOs = [&](const std::vector<uint8_t>& blob, uint32_t start, uint32_t end) {
            struct PO { uint32_t pos; uint32_t val; };
            std::vector<PO> out;
            if (end == 0 || end > (uint32_t)blob.size()) end = (uint32_t)blob.size();
            for (uint32_t p = start; p + 12 <= end; ++p) {
                if (blob[p] != 0xFF) continue;
                if (memcmp(blob.data() + p, SENT8, 8) != 0) continue;
                uint32_t po_pos = p + 8, po_val = 0;
                memcpy(&po_val, blob.data() + po_pos, 4);
                if (po_val == po_pos + 4) out.push_back({po_pos, po_val});
            }
            return out;
        };

        // Process each block sequentially (sorted by offset descending to avoid cascading shifts)
        struct Transplant { std::string name; uint32_t tgt_off; };
        std::vector<Transplant> todo;
        for (auto& bn : block_names) {
            const SaveParserCpp::ObjectBlock* tb = nullptr;
            for (auto& obj : target.parsed.objects) { if (obj.class_name == bn) { tb = &obj; break; } }
            const SaveParserCpp::ObjectBlock* db = nullptr;
            for (auto& obj : donor.parsed.objects) { if (obj.class_name == bn) { db = &obj; break; } }
            if (!tb) { fprintf(stderr, "SKIP '%s': not in target\n", bn.c_str()); continue; }
            if (!db) { fprintf(stderr, "SKIP '%s': not in donor\n", bn.c_str()); continue; }
            todo.push_back({bn, tb->data_offset});
        }
        // Sort by offset descending so later splices don't invalidate earlier offsets
        std::sort(todo.begin(), todo.end(), [](auto& a, auto& b) { return a.tgt_off > b.tgt_off; });

        int total_ok = 0;
        for (auto& t : todo) {
            // Re-find blocks after each reparse
            const SaveParserCpp::ObjectBlock* tb = nullptr;
            for (auto& obj : target.parsed.objects) { if (obj.class_name == t.name) { tb = &obj; break; } }
            const SaveParserCpp::ObjectBlock* db = nullptr;
            for (auto& obj : donor.parsed.objects) { if (obj.class_name == t.name) { db = &obj; break; } }
            if (!tb || !db) continue;

            fprintf(stderr, "\n--- Transplanting %s ---\n", t.name.c_str());
            fprintf(stderr, "  Target: off=0x%X sz=%u | Donor: off=0x%X sz=%u\n",
                tb->data_offset, tb->data_size, db->data_offset, db->data_size);

            // Extract donor bytes
            std::vector<uint8_t> donorBytes(donor.blob.begin() + db->data_offset,
                                            donor.blob.begin() + db->data_offset + db->data_size);

            // Remap type indices in donor bytes
            auto donorPOs = collectPOs(donor.blob, db->data_offset, db->data_offset + db->data_size);
            int remapped = 0;
            for (auto& op : donorPOs) {
                uint32_t rel = op.pos - db->data_offset;
                if (rel < 11 || rel + 4 > donorBytes.size()) continue;
                uint16_t ti = (uint16_t)donorBytes[rel-11] | ((uint16_t)donorBytes[rel-10] << 8);
                auto it = remap.find(ti);
                if (it != remap.end() && it->second != ti) {
                    donorBytes[rel-11] = (uint8_t)(it->second & 0xFF);
                    donorBytes[rel-10] = (uint8_t)((it->second >> 8) & 0xFF);
                    remapped++;
                }
            }

            // Rebase POs to target position
            int rebased = 0;
            for (auto& op : donorPOs) {
                uint32_t rel = op.pos - db->data_offset;
                if (rel + 4 > donorBytes.size()) continue;
                uint32_t new_po = tb->data_offset + rel + 4;
                memcpy(donorBytes.data() + rel, &new_po, 4);
                rebased++;
            }

            // Collect target's full PO table BEFORE splice
            auto targetPOs = collectPOs(target.blob, 0, 0);

            uint32_t old_start = tb->data_offset;
            uint32_t old_end = old_start + tb->data_size;
            uint32_t new_size = (uint32_t)donorBytes.size();
            int32_t delta = (int32_t)new_size - (int32_t)tb->data_size;

            // Splice
            target.blob.erase(target.blob.begin() + old_start, target.blob.begin() + old_end);
            target.blob.insert(target.blob.begin() + old_start, donorBytes.begin(), donorBytes.end());

            // Fix surrounding POs if delta != 0
            if (delta != 0) {
                int po_fixed = 0;
                for (auto& op : targetPOs) {
                    if (op.pos >= old_start && op.pos < old_end) continue;
                    uint32_t new_pos = op.pos >= old_end ? (uint32_t)((int32_t)op.pos + delta) : op.pos;
                    if (new_pos + 4 > target.blob.size()) continue;
                    if (op.val >= old_end) {
                        uint32_t new_val = (uint32_t)((int32_t)op.val + delta);
                        memcpy(target.blob.data() + new_pos, &new_val, 4);
                        po_fixed++;
                    }
                }
                fprintf(stderr, "  PO fixup: %d surrounding POs adjusted\n", po_fixed);
            }

            // TOC fixup
            auto classIt = remap.find((uint16_t)db->class_index);
            uint32_t newCI = classIt != remap.end() ? (uint32_t)classIt->second : db->class_index;
            uint32_t toc_start = target.parsed.schema.schema_end + 12;
            for (size_t i = 0; i < target.parsed.toc.entries.size(); i++) {
                uint32_t ep = toc_start + (uint32_t)(i * 20);
                uint32_t doff = 0, dsz = 0;
                memcpy(&doff, target.blob.data() + ep + 12, 4);
                memcpy(&dsz, target.blob.data() + ep + 16, 4);
                if (i == tb->entry_index) {
                    memcpy(target.blob.data() + ep, &newCI, 4);
                    memcpy(target.blob.data() + ep + 16, &new_size, 4);
                } else if (doff > old_start && delta != 0) {
                    uint32_t new_doff = (uint32_t)((int32_t)doff + delta);
                    memcpy(target.blob.data() + ep + 12, &new_doff, 4);
                }
            }
            uint32_t ss = (uint32_t)target.blob.size();
            memcpy(target.blob.data() + target.parsed.schema.schema_end + 8, &ss, 4);

            fprintf(stderr, "  %d type indices remapped, %d POs rebased, delta=%d\n", remapped, rebased, delta);

            // Reparse for next iteration
            Reparse(target, false);
            total_ok++;

            // Verify this block
            for (auto& obj : target.parsed.objects) {
                if (obj.class_name == t.name) {
                    int elems = 0;
                    for (auto& f : obj.fields) elems += (int)f.list_elements.size();
                    fprintf(stderr, "  Result: off=0x%X sz=%u elems=%d\n", obj.data_offset, obj.data_size, elems);
                    break;
                }
            }
        }

        // Final roundtrip verification
        fprintf(stderr, "\n=== Final verification ===\n");
        auto fresh = ParcSerializer::Serialize(target.parsed, target.blob);
        int rt_diffs = 0;
        if (fresh.size() == target.blob.size()) {
            for (size_t i = 0; i < fresh.size(); i++)
                if (fresh[i] != target.blob[i]) rt_diffs++;
            fprintf(stderr, "Reserialize: %s (%d diffs)\n",
                rt_diffs == 0 ? "PERFECT MATCH" : "MISMATCH", rt_diffs);
        } else {
            fprintf(stderr, "Reserialize: SIZE MISMATCH %zu vs %zu\n", target.blob.size(), fresh.size());
        }

        WriteSave(target, output);
        fprintf(stderr, "Written: %s (%zu bytes)\n", output, target.blob.size());
        fprintf(stderr, "%d/%zu blocks transplanted\n", total_ok, block_names.size());
        return 0;
    }

    // ── RESERIALIZE ──
    if (strcmp(cmd, "reserialize") == 0) {
        if (argc < 4) { fprintf(stderr, "Usage: reserialize <save> <output>\n"); return 1; }
        const char* output = argv[3];

        auto tree = LoadSave(save_path);
        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Loaded in %.1f ms\n",
            std::chrono::duration<double, std::milli>(t1 - t0).count());

        auto new_blob = ParcSerializer::Serialize(tree.parsed, tree.blob);
        auto t2 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Reserialized: %zu -> %zu bytes (%.1f ms)\n",
            tree.blob.size(), new_blob.size(),
            std::chrono::duration<double, std::milli>(t2 - t1).count());

        // Compare with original
        if (new_blob.size() == tree.blob.size()) {
            int diffs = 0;
            int first_diff = -1;
            for (size_t i = 0; i < new_blob.size(); ++i) {
                if (new_blob[i] != tree.blob[i]) {
                    if (first_diff < 0) first_diff = (int)i;
                    ++diffs;
                }
            }
            if (diffs == 0) {
                fprintf(stderr, "PERFECT MATCH — byte-identical to original!\n");
            } else {
                fprintf(stderr, "DIFFS: %d bytes differ (first at offset 0x%X)\n", diffs, first_diff);
                // Show first 20 diff locations with context
                int shown = 0;
                for (size_t i = 0; i < new_blob.size() && shown < 20; ++i) {
                    if (new_blob[i] != tree.blob[i]) {
                        // Check if this is a 4-byte PO diff (sentinel 8 bytes before)
                        bool is_po = (i >= 8 && memcmp(tree.blob.data() + i - 8, "\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF", 8) == 0);
                        uint32_t orig_val = 0, new_val = 0;
                        if (i + 4 <= new_blob.size()) {
                            memcpy(&orig_val, tree.blob.data() + i, 4);
                            memcpy(&new_val, new_blob.data() + i, 4);
                        }
                        // Find which block/field contains this offset
                        const char* block_name = "?";
                        uint32_t block_off = 0;
                        for (auto& obj : tree.parsed.objects) {
                            if (i >= obj.data_offset && i < obj.data_offset + obj.data_size) {
                                block_name = obj.class_name.c_str();
                                block_off = (uint32_t)(i - obj.data_offset);
                                break;
                            }
                        }
                        fprintf(stderr, "  diff[%d] @0x%zX: orig=0x%08X new=0x%08X %s [%s +0x%X]\n",
                            shown, i, orig_val, new_val, is_po ? "PO" : "", block_name, block_off);
                        i += 3; // skip rest of u32
                        shown++;
                    }
                }
            }
        } else {
            fprintf(stderr, "SIZE MISMATCH: original=%zu reserialized=%zu (delta=%d)\n",
                tree.blob.size(), new_blob.size(),
                (int)new_blob.size() - (int)tree.blob.size());

            // Per-block size comparison
            int blocks_diff = 0;
            int64_t total_delta = 0;
            for (size_t bi = 0; bi < tree.parsed.objects.size() && bi < tree.parsed.toc.entries.size(); ++bi) {
                auto& obj = tree.parsed.objects[bi];
                // Read new block size from TOC in new_blob
                uint32_t toc_base = tree.parsed.schema.schema_end + 12;
                uint32_t new_off = 0, new_sz = 0;
                if (toc_base + bi * 20 + 20 <= new_blob.size()) {
                    memcpy(&new_off, new_blob.data() + toc_base + bi * 20 + 12, 4);
                    memcpy(&new_sz, new_blob.data() + toc_base + bi * 20 + 16, 4);
                }
                int32_t d = (int32_t)new_sz - (int32_t)obj.data_size;
                total_delta += d;
                if (d != 0) {
                    blocks_diff++;
                    if (blocks_diff <= 10) {
                        fprintf(stderr, "  block[%zu] %s: %u -> %u (delta=%d) fields=%zu undecoded=%zu\n",
                            bi, obj.class_name.c_str(), obj.data_size, new_sz, d,
                            obj.fields.size(), obj.undecoded_ranges.size());
                        for (auto& [us, ue] : obj.undecoded_ranges) {
                            fprintf(stderr, "    undecoded: [0x%X..0x%X] (%u bytes)\n", us, ue, ue - us);
                        }
                        uint32_t hdr = 2 + (uint32_t)obj.header_mask_bytes.size() + 4;
                        uint32_t cur = obj.data_offset + hdr;
                        for (size_t fi = 0; fi < obj.fields.size(); ++fi) {
                            auto& fld = obj.fields[fi];
                            fprintf(stderr, "    field[%zu] %s k=%u p=%d off=[0x%X..0x%X] rv=%zu\n",
                                fi, fld.name.c_str(), fld.meta_kind, fld.present?1:0,
                                fld.start_offset, fld.end_offset, fld.raw_value.size());
                        }
                    }
                }
            }
            fprintf(stderr, "  %d blocks differ, total block delta=%lld\n", blocks_diff, (long long)total_delta);
        }

        // Write output
        tree.blob = std::move(new_blob);
        WriteSave(tree, output);
        auto t3 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Written: %s (%.1f ms)\n", output,
            std::chrono::duration<double, std::milli>(t3 - t2).count());
        fprintf(stderr, "Total: %.1f ms\n",
            std::chrono::duration<double, std::milli>(t3 - t0).count());
        return 0;
    }

    // ── INSERT ──
    if (strcmp(cmd, "insert") == 0) {
        if (argc < 6) {
            fprintf(stderr, "Usage: insert <save> <block> <field> <template.bin|hex:AABB...> [-o out.save]\n");
            return 1;
        }
        const char* block_class = argv[3];
        const char* list_field = argv[4];
        const char* tmpl_arg = argv[5];
        const char* output = save_path;

        for (int i = 6; i < argc; ++i) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[++i];
        }

        auto tree = LoadSave(save_path);

        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Loaded in %.1f ms, POs: %zu, trailing: %zu\n",
            std::chrono::duration<double, std::milli>(t1 - t0).count(),
            tree.po_table.size(), tree.ts_table.size());

        // Load template
        std::vector<uint8_t> tmpl;
        if (strncmp(tmpl_arg, "hex:", 4) == 0) {
            tmpl = HexToBytes(tmpl_arg + 4);
        } else {
            tmpl = ReadBinaryFile(tmpl_arg);
        }
        fprintf(stderr, "Template: %zu bytes\n", tmpl.size());

        // Adapt type indices from reference element (copies from last existing
        // element in the target list — no role guessing, matches Python editor)
        AdaptTypeIndicesFromReference(tmpl, tree.blob, tree, block_class, list_field);
        fprintf(stderr, "Type indices adapted from reference element\n");

        // Byte-splice insertion (no tree reconstruction)
        auto t2 = std::chrono::high_resolution_clock::now();
        auto result = SpliceIntoList(tree, block_class, list_field, tmpl);
        auto t3 = std::chrono::high_resolution_clock::now();

        if (!result.ok) {
            fprintf(stderr, "ERROR: %s\n", result.error.c_str());
            return 1;
        }

        fprintf(stderr, "Inserted: index=%d, offset=0x%X, growth=%d, POs fixed=%d (%.1f ms)\n",
            result.new_element_index, result.insert_offset, result.growth, result.po_fixed,
            std::chrono::duration<double, std::milli>(t3 - t2).count());

        // Write
        auto t4 = std::chrono::high_resolution_clock::now();
        WriteSave(tree, output);
        auto t5 = std::chrono::high_resolution_clock::now();

        fprintf(stderr, "Written: %s (%.1f ms)\n", output,
            std::chrono::duration<double, std::milli>(t5 - t4).count());
        fprintf(stderr, "Total: %.1f ms\n",
            std::chrono::duration<double, std::milli>(t5 - t0).count());
        return 0;
    }

    // ── REMOVE ──
    if (strcmp(cmd, "remove") == 0) {
        if (argc < 6) {
            fprintf(stderr, "Usage: remove <save> <block> <field> <index> [-o out.save]\n");
            return 1;
        }
        const char* block_class = argv[3];
        const char* list_field = argv[4];
        int index = atoi(argv[5]);
        const char* output = save_path;

        for (int i = 6; i < argc; ++i) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[++i];
        }

        auto tree = LoadSave(save_path);
        auto result = RemoveFromList(tree, block_class, list_field, index);

        if (!result.ok) {
            fprintf(stderr, "ERROR: %s\n", result.error.c_str());
            return 1;
        }

        fprintf(stderr, "Removed: index=%d, shrink=%d, POs fixed=%d\n",
            index, result.shrink, result.po_fixed);

        WriteSave(tree, output);
        fprintf(stderr, "Written: %s\n", output);
        return 0;
    }

    // ── FULLPATH ── dump every unique field path in the save (field names at all depths)
    if (strcmp(cmd, "fullpath") == 0) {
        auto tree = LoadSave(save_path);
        std::set<std::string> paths;
        int total_fields = 0;
        int named_fields = 0;
        int skipped_fields = 0;

        struct FPWalker {
            std::set<std::string>& paths;
            int& total_fields;
            int& named_fields;
            int& skipped_fields;
            void walk(const std::vector<SaveParserCpp::GenericFieldValue>& fields, const std::string& prefix) {
                for (auto& f : fields) {
                    if (!f.present) continue;
                    total_fields++;
                    std::string p = prefix + "." + f.name;
                    if (f.name.empty() || f.name[0] == '[') {
                        // list element — use type name
                        p = prefix + ".[" + f.child_type_name + "]";
                    } else {
                        named_fields++;
                    }
                    if (f.decode_kind == "skipped") skipped_fields++;
                    std::string kind_str;
                    switch(f.meta_kind) {
                        case 0: case 2: kind_str = "scalar"; break;
                        case 1: kind_str = "bytes"; break;
                        case 3: kind_str = "array"; break;
                        case 4: case 5: kind_str = "object"; break;
                        case 6: case 7: kind_str = "list[" + std::to_string(f.list_elements.size()) + "]"; break;
                        default: kind_str = "?"; break;
                    }
                    paths.insert(p + " (" + kind_str + ")");
                    // Recurse into children
                    if (!f.child_fields.empty()) {
                        walk(f.child_fields, p);
                    }
                    // For lists, recurse into first element to show structure
                    if (!f.list_elements.empty()) {
                        walk(f.list_elements[0].child_fields, p + ".[" + f.list_elements[0].child_type_name + "]");
                    }
                }
            }
        };
        FPWalker w{paths, total_fields, named_fields, skipped_fields};
        for (auto& obj : tree.parsed.objects) {
            w.walk(obj.fields, obj.class_name);
        }
        for (auto& p : paths) fprintf(stderr, "%s\n", p.c_str());
        fprintf(stderr, "\n--- %zu unique paths, %d total present fields, %d named, %d skipped ---\n",
            paths.size(), total_fields, named_fields, skipped_fields);
        return 0;
    }

    // ── TREEDIAG ── deep tree diagnostic for a block (or all blocks)
    if (strcmp(cmd, "treediag") == 0) {
        const char* target = (argc > 3) ? argv[3] : "*";
        auto tree = LoadSave(save_path);
        int partial_count = 0;
        int total_lists = 0;

        // Recursive lambda to walk all fields
        struct Walker {
            int& partial_count;
            int& total_lists;
            void walk(const std::vector<SaveParserCpp::GenericFieldValue>& fields, int depth, const std::string& path) {
                for (auto& f : fields) {
                    if (!f.present) continue;
                    if (f.meta_kind == 6 || f.meta_kind == 7) {
                        total_lists++;
                        bool partial = (f.list_elements.size() < f.list_count && f.list_count > 0);
                        if (partial) {
                            partial_count++;
                            fprintf(stderr, "  PARTIAL: %s.%s: %zu/%u elems\n",
                                path.c_str(), f.name.c_str(),
                                f.list_elements.size(), f.list_count);
                        }
                        for (size_t ei = 0; ei < f.list_elements.size(); ei++) {
                            auto& e = f.list_elements[ei];
                            std::string epath = path + "." + f.name + "[" + std::to_string(ei) + "]";
                            walk(e.child_fields, depth + 1, epath);
                        }
                    } else if (f.meta_kind == 4 || f.meta_kind == 5) {
                        walk(f.child_fields, depth + 1, path + "." + f.name);
                    }
                }
            }
        };
        Walker w{partial_count, total_lists};

        for (auto& obj : tree.parsed.objects) {
            if (strcmp(target, "*") != 0 && obj.class_name.find(target) == std::string::npos) continue;
            w.walk(obj.fields, 0, obj.class_name);
        }
        fprintf(stderr, "\nSummary: %d total lists, %d PARTIAL decodes\n", total_lists, partial_count);
        return 0;
    }

    // ── CRAFTITEM ── construct an item from scratch and insert into save
    if (strcmp(cmd, "craftitem") == 0) {
        if (argc < 4) {
            fprintf(stderr, "Usage: craftitem <save> <itemKey> [enchant=N] [sockets=N] [stack=N] [cat=N] [-o out.save]\n"
                "  Creates an item from scratch with the given itemKey and inserts it.\n"
                "  Example: craftitem save.save 12218 enchant=10 sockets=5\n");
            return 1;
        }
        uint32_t itemKey = (uint32_t)atoi(argv[3]);
        uint16_t enchant = 0;
        uint8_t sockets = 0;
        uint64_t stack = 1;
        int category = 1;
        const char* output = save_path;

        for (int i = 4; i < argc; i++) {
            if (strncmp(argv[i], "enchant=", 8) == 0) enchant = (uint16_t)atoi(argv[i]+8);
            else if (strncmp(argv[i], "sockets=", 8) == 0) sockets = (uint8_t)atoi(argv[i]+8);
            else if (strncmp(argv[i], "stack=", 6) == 0) stack = (uint64_t)atoll(argv[i]+6);
            else if (strncmp(argv[i], "cat=", 4) == 0) category = atoi(argv[i]+4);
            else if (strcmp(argv[i], "-o") == 0 && i+1 < argc) output = argv[++i];
        }

        auto tree = LoadSave(save_path);

        // Build type_index map from target schema
        std::unordered_map<std::string, uint16_t> ti_map;
        for (size_t i = 0; i < tree.parsed.schema.types.size(); i++)
            ti_map[tree.parsed.schema.types[i].name] = (uint16_t)i;

        // Find max itemNo in the save (for unique ID assignment)
        uint64_t max_item_no = 1000;
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name.find("InventorySaveData") == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist") continue;
                for (auto& cat_elem : f.list_elements) {
                    for (auto& cf : cat_elem.child_fields) {
                        if (cf.name == "_itemList") {
                            for (auto& item : cf.list_elements) {
                                for (auto& ifld : item.child_fields) {
                                    if (ifld.name == "_itemNo" && ifld.raw_value.size() == 8) {
                                        uint64_t no = 0;
                                        memcpy(&no, ifld.raw_value.data(), 8);
                                        if (no > max_item_no) max_item_no = no;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            break;
        }

        ItemFactory::ItemSpec spec;
        spec.itemKey = itemKey;
        spec.itemNo = max_item_no + 1;
        spec.slotNo = 999; // game assigns proper slot
        spec.stackCount = stack;
        spec.enchantLevel = enchant;
        spec.endurance = 65535;
        spec.sharpness = (enchant > 0) ? 25 : 0;
        spec.maxSocketCount = sockets;
        for (uint8_t s = 0; s < sockets; s++) {
            spec.sockets.push_back({0, 65535}); // empty socket
        }

        auto item_bytes = ItemFactory::BuildItem(spec, ti_map);
        if (item_bytes.empty()) {
            fprintf(stderr, "ERROR: Cannot build item (missing ItemSaveData in schema?)\n");
            return 1;
        }

        fprintf(stderr, "Built item: key=%u no=%llu enchant=%u sockets=%u (%zu bytes)\n",
            spec.itemKey, (unsigned long long)spec.itemNo, spec.enchantLevel,
            spec.maxSocketCount, item_bytes.size());

        // Insert into inventory category
        char path[128];
        snprintf(path, sizeof(path), "_inventorylist[%d]._itemList", category);
        auto result = InsertNested(tree, "InventorySaveData", path, item_bytes, -1);
        if (!result.ok) {
            fprintf(stderr, "ERROR: %s\n", result.error.c_str());
            return 1;
        }
        fprintf(stderr, "Inserted at index %d into category %d\n", result.new_element_index, category);

        WriteSave(tree, output);
        fprintf(stderr, "Written: %s\n", output);

        // Verify
        auto tree2 = LoadSave(output);
        for (auto& obj : tree2.parsed.objects) {
            if (obj.class_name.find("InventorySaveData") == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist" || category >= (int)f.list_elements.size()) continue;
                for (auto& cf : f.list_elements[category].child_fields) {
                    if (cf.name == "_itemList")
                        fprintf(stderr, "Reload: category[%d] has %zu items\n", category, cf.list_elements.size());
                }
            }
            break;
        }
        return 0;
    }

    // ── ITEMXFER ── transfer a single item from donor save to target save
    if (strcmp(cmd, "itemxfer") == 0) {
        if (argc < 5) {
            fprintf(stderr, "Usage: itemxfer <target_save> <donor_save> <donor_category_idx> [-o out.save]\n"
                "  Copies the last item from donor's _inventorylist[idx]._itemList into target.\n");
            return 1;
        }
        const char* donor_path = argv[3];
        int donor_cat = atoi(argv[4]);
        const char* output = save_path;
        for (int i = 5; i < argc; ++i) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[++i];
        }

        auto target = LoadSave(save_path);
        auto donor = LoadSave(donor_path);

        // Build type name→index maps for both schemas
        std::unordered_map<uint16_t, std::string> donor_ti_to_name;
        for (size_t i = 0; i < donor.parsed.schema.types.size(); i++)
            donor_ti_to_name[(uint16_t)i] = donor.parsed.schema.types[i].name;
        std::unordered_map<std::string, uint16_t> target_name_to_ti;
        for (size_t i = 0; i < target.parsed.schema.types.size(); i++)
            target_name_to_ti[target.parsed.schema.types[i].name] = (uint16_t)i;

        // Find the donor item
        std::vector<uint8_t> item_bytes;
        for (auto& obj : donor.parsed.objects) {
            if (obj.class_name.find("InventorySaveData") == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist") continue;
                if (donor_cat >= (int)f.list_elements.size()) {
                    fprintf(stderr, "ERROR: donor category %d out of range\n", donor_cat);
                    return 1;
                }
                auto& cat = f.list_elements[donor_cat];
                for (auto& cf : cat.child_fields) {
                    if (cf.name == "_itemList" && !cf.list_elements.empty()) {
                        item_bytes = cf.list_elements.back().raw_value;
                        fprintf(stderr, "Donor item: %zu bytes from category[%d] (last of %zu items)\n",
                            item_bytes.size(), donor_cat, cf.list_elements.size());
                    }
                }
            }
            break;
        }
        if (item_bytes.empty()) {
            fprintf(stderr, "ERROR: No items found in donor category %d\n", donor_cat);
            return 1;
        }

        // Remap type indices in the item bytes (donor→target by name)
        static const uint8_t SENT8[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
        int remapped = 0, missing = 0;
        // Main type_index at offset 2+MBC
        uint16_t mbc = 0;
        memcpy(&mbc, item_bytes.data(), 2);
        {
            uint16_t ti = 0;
            memcpy(&ti, item_bytes.data() + 2 + mbc, 2);
            auto it = donor_ti_to_name.find(ti);
            if (it != donor_ti_to_name.end()) {
                auto tgt = target_name_to_ti.find(it->second);
                if (tgt != target_name_to_ti.end()) {
                    uint16_t new_ti = tgt->second;
                    memcpy(item_bytes.data() + 2 + mbc, &new_ti, 2);
                    fprintf(stderr, "Remapped main type: %s (%u -> %u)\n", it->second.c_str(), ti, new_ti);
                    remapped++;
                } else { missing++; fprintf(stderr, "MISSING type in target: %s\n", it->second.c_str()); }
            }
        }
        // Nested type indices — start AFTER main header to avoid re-remapping main type
        uint32_t item_hdr = 2 + mbc + 2 + 1 + 8 + 4;
        for (size_t p = item_hdr; p + 8 <= item_bytes.size(); ++p) {
            if (memcmp(item_bytes.data() + p, SENT8, 8) != 0) continue;
            if (p < 3) continue;
            uint16_t ti = 0;
            memcpy(&ti, item_bytes.data() + p - 3, 2);
            auto it = donor_ti_to_name.find(ti);
            if (it != donor_ti_to_name.end()) {
                auto tgt = target_name_to_ti.find(it->second);
                if (tgt != target_name_to_ti.end()) {
                    uint16_t new_ti = tgt->second;
                    memcpy(item_bytes.data() + p - 3, &new_ti, 2);
                    fprintf(stderr, "Remapped nested type: %s (%u -> %u)\n", it->second.c_str(), ti, new_ti);
                    remapped++;
                } else { missing++; fprintf(stderr, "MISSING nested type: %s\n", it->second.c_str()); }
            }
        }
        fprintf(stderr, "Remapped %d type indices, %d missing\n", remapped, missing);
        if (missing > 0) {
            fprintf(stderr, "ERROR: Target schema missing required types\n");
            return 1;
        }

        // Find a target category that has items (to insert into)
        // Use category 1 (usually equipment/main inventory)
        std::string insert_path = "_inventorylist[1]._itemList";
        fprintf(stderr, "Inserting into %s ...\n", insert_path.c_str());

        // Set start_offset from an existing item in the target (for FixPayloadPOs validation)
        for (auto& obj : target.parsed.objects) {
            if (obj.class_name.find("InventorySaveData") == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist") continue;
                if (f.list_elements.size() > 1) {
                    for (auto& cf : f.list_elements[1].child_fields) {
                        if (cf.name == "_itemList" && !cf.list_elements.empty()) {
                            // Use an existing item's start_offset as reference
                            // (needed for FixPayloadPOs _is_real_po validation)
                        }
                    }
                }
            }
            break;
        }

        auto result = InsertNested(target, "InventorySaveData", insert_path, item_bytes, -1);
        if (!result.ok) {
            fprintf(stderr, "ERROR: %s\n", result.error.c_str());
            return 1;
        }
        fprintf(stderr, "Inserted at index %d\n", result.new_element_index);

        WriteSave(target, output);
        fprintf(stderr, "Written: %s\n", output);

        // Validate
        auto tree2 = LoadSave(output);
        fprintf(stderr, "Reload: %zu objects\n", tree2.parsed.objects.size());
        for (auto& obj : tree2.parsed.objects) {
            if (obj.class_name.find("InventorySaveData") == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist") continue;
                if (f.list_elements.size() > 1) {
                    for (auto& cf : f.list_elements[1].child_fields) {
                        if (cf.name == "_itemList")
                            fprintf(stderr, "Target _itemList[1]: %zu items\n", cf.list_elements.size());
                    }
                }
            }
            break;
        }
        return 0;
    }

    // ── TRANSPLANT ── replace an entire list from a donor save into a target save
    if (strcmp(cmd, "transplant") == 0) {
        if (argc < 6) {
            fprintf(stderr, "Usage: transplant <target_save> <donor_save> <block> <field_path> [-o out.save]\n"
                "  Replaces the entire list at field_path in target with the one from donor.\n"
                "  Example: transplant beginner.save endgame.save InventorySaveData _inventorylist\n");
            return 1;
        }
        const char* donor_path = argv[3];
        const char* block_class = argv[4];
        const char* field_path = argv[5];
        const char* output = save_path;
        for (int i = 6; i < argc; ++i) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[++i];
        }

        fprintf(stderr, "Loading target: %s\n", save_path);
        auto target = LoadSave(save_path);
        fprintf(stderr, "Loading donor: %s\n", donor_path);
        auto donor = LoadSave(donor_path);

        fprintf(stderr, "Target schema: %zu types\n", target.parsed.schema.types.size());
        fprintf(stderr, "Donor schema: %zu types\n", donor.parsed.schema.types.size());

        // Find the target list field
        GenericFieldValue* target_field = nullptr;
        ObjectBlock* target_block = nullptr;
        for (auto& obj : target.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            target_block = &obj;
            for (auto& f : obj.fields) {
                if (f.name == field_path) { target_field = &f; break; }
            }
            break;
        }
        if (!target_field) {
            fprintf(stderr, "ERROR: Cannot find %s.%s in target\n", block_class, field_path);
            return 1;
        }

        // Find the donor list field
        GenericFieldValue* donor_field = nullptr;
        for (auto& obj : donor.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name == field_path) { donor_field = &f; break; }
            }
            break;
        }
        if (!donor_field) {
            fprintf(stderr, "ERROR: Cannot find %s.%s in donor\n", block_class, field_path);
            return 1;
        }

        fprintf(stderr, "Target: %s has %zu elements (rv=%zu)\n",
            field_path, target_field->list_elements.size(), target_field->raw_value.size());
        fprintf(stderr, "Donor: %s has %zu elements (rv=%zu)\n",
            field_path, donor_field->list_elements.size(), donor_field->raw_value.size());

        // If donor has MORE types, upgrade target schema (donor is a superset).
        if (donor.parsed.schema.types.size() > target.parsed.schema.types.size()) {
            fprintf(stderr, "Upgrading target schema (%zu -> %zu types)...\n",
                target.parsed.schema.types.size(), donor.parsed.schema.types.size());
            uint32_t old_end = target.parsed.schema.schema_end;
            uint32_t new_end = donor.parsed.schema.schema_end;
            int32_t schema_delta = (int32_t)new_end - (int32_t)old_end;

            // Build new blob: donor schema + target TOC/data (with fixed offsets)
            std::vector<uint8_t> new_blob;
            new_blob.reserve(target.blob.size() + schema_delta);
            // 1. Copy donor schema
            new_blob.insert(new_blob.end(), donor.blob.begin(), donor.blob.begin() + new_end);
            // 2. Copy target TOC + blocks (everything after old schema)
            new_blob.insert(new_blob.end(), target.blob.begin() + old_end, target.blob.end());

            // 3. Fix TOC offsets: shift data_offset by schema_delta
            uint32_t toc_start = new_end + 12; // after TOC header (prefix_zero + entry_count + stream_size)
            uint32_t entry_count = target.parsed.toc.entries.size();
            for (uint32_t i = 0; i < entry_count; i++) {
                uint32_t entry_pos = toc_start + i * 20 + 12; // data_offset field
                if (entry_pos + 4 <= new_blob.size()) {
                    uint32_t old_off = 0;
                    memcpy(&old_off, new_blob.data() + entry_pos, 4);
                    uint32_t new_off = (uint32_t)((int32_t)old_off + schema_delta);
                    memcpy(new_blob.data() + entry_pos, &new_off, 4);
                }
            }
            // 4. Fix stream_size
            uint32_t stream_size_pos = new_end + 8;
            uint32_t new_stream_size = (uint32_t)new_blob.size();
            memcpy(new_blob.data() + stream_size_pos, &new_stream_size, 4);

            target.blob = std::move(new_blob);
            fprintf(stderr, "Schema delta: %d bytes. New blob: %zu bytes\n",
                schema_delta, target.blob.size());
            Reparse(target, false);
            fprintf(stderr, "Reparsed: %zu objects\n", target.parsed.objects.size());

            // Re-find fields after reparse
            target_field = nullptr;
            for (auto& obj : target.parsed.objects) {
                if (obj.class_name.find(block_class) == std::string::npos) continue;
                for (auto& f : obj.fields) {
                    if (f.name == field_path) { target_field = &f; break; }
                }
                break;
            }
            if (!target_field) { fprintf(stderr, "ERROR: lost target field\n"); return 1; }
        }

        // Check schema compatibility: compare type names for the list's element type
        if (!target_field->list_elements.empty() && !donor_field->list_elements.empty()) {
            auto& tt = target_field->list_elements[0].child_type_name;
            auto& dt = donor_field->list_elements[0].child_type_name;
            fprintf(stderr, "Target element type: %s\n", tt.c_str());
            fprintf(stderr, "Donor element type: %s\n", dt.c_str());
            if (tt != dt) {
                fprintf(stderr, "WARNING: Element types differ! Proceeding anyway...\n");
            }
        }

        // Save original field position BEFORE modification
        uint32_t orig_field_start = target_field->start_offset;
        uint32_t orig_field_end = target_field->end_offset;
        fprintf(stderr, "Original field position: [0x%X..0x%X] (%u bytes)\n",
            orig_field_start, orig_field_end, orig_field_end - orig_field_start);

        // Replace: copy donor's list_elements and list metadata into target
        target_field->list_elements = donor_field->list_elements;
        target_field->list_count = (uint32_t)donor_field->list_elements.size();
        // Copy list_header_raw from donor (it has the correct format for the donor's count)
        if (!donor_field->list_header_raw.empty()) {
            target_field->list_header_raw = donor_field->list_header_raw;
        }
        // Clear raw_value so serializer reconstructs from the new elements
        target_field->raw_value.clear();

        fprintf(stderr, "Transplanted %zu elements. Serializing...\n", target_field->list_elements.size());

        // Verify the tree actually has the transplanted data
        for (auto& obj : target.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name == field_path) {
                    fprintf(stderr, "[VERIFY] Tree field: elems=%zu rv=%zu lhr=%zu lcount=%u\n",
                        f.list_elements.size(), f.raw_value.size(),
                        f.list_header_raw.size(), f.list_count);
                    if (!f.list_elements.empty()) {
                        fprintf(stderr, "[VERIFY] elem[0].rv=%zu elem[0].cf=%zu\n",
                            f.list_elements[0].raw_value.size(),
                            f.list_elements[0].child_fields.size());
                    }
                }
            }
            break;
        }

        // Cross-save transplant: remap type_index values from donor schema to target schema.
        // Build name→index map for both schemas.
        std::unordered_map<uint16_t, std::string> donor_idx_to_name;
        for (size_t i = 0; i < donor.parsed.schema.types.size(); i++)
            donor_idx_to_name[(uint16_t)i] = donor.parsed.schema.types[i].name;

        std::unordered_map<std::string, uint16_t> target_name_to_idx;
        for (size_t i = 0; i < target.parsed.schema.types.size(); i++)
            target_name_to_idx[target.parsed.schema.types[i].name] = (uint16_t)i;

        int remapped = 0, missing_types = 0;
        static const uint8_t SENT8[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};

        // Remap type indices in each element's raw_value bytes.
        // Type index is at offset 2+MBC within the element header, and at sentinel-3
        // for nested inline objects.
        auto remap_element = [&](GenericFieldValue& elem) {
            if (elem.raw_value.empty()) return;
            uint16_t mbc = elem.child_mask_byte_count;
            // Remap main type_index in header
            uint16_t donor_ti = 0;
            memcpy(&donor_ti, elem.raw_value.data() + 2 + mbc, 2);
            auto it = donor_idx_to_name.find(donor_ti);
            if (it != donor_idx_to_name.end()) {
                auto tgt = target_name_to_idx.find(it->second);
                if (tgt != target_name_to_idx.end()) {
                    uint16_t new_ti = tgt->second;
                    memcpy(elem.raw_value.data() + 2 + mbc, &new_ti, 2);
                    elem.child_type_index = new_ti;
                    remapped++;
                } else {
                    missing_types++;
                }
            }
            // Remap nested type indices (at each sentinel in the raw bytes)
            for (size_t p = 2 + mbc + 3; p + 8 <= elem.raw_value.size(); ++p) {
                if (memcmp(elem.raw_value.data() + p, SENT8, 8) != 0) continue;
                // Type index is 3 bytes before sentinel (at p-3)
                if (p < 3) continue;
                uint16_t nested_ti = 0;
                memcpy(&nested_ti, elem.raw_value.data() + p - 3, 2);
                auto nit = donor_idx_to_name.find(nested_ti);
                if (nit != donor_idx_to_name.end()) {
                    auto ntgt = target_name_to_idx.find(nit->second);
                    if (ntgt != target_name_to_idx.end()) {
                        uint16_t new_nti = ntgt->second;
                        memcpy(elem.raw_value.data() + p - 3, &new_nti, 2);
                        remapped++;
                    } else {
                        missing_types++;
                    }
                }
            }
            // Clear child_fields so serializer uses raw_value fallback (self-contained)
            elem.child_fields.clear();
            elem.child_payload_offset = 0;
        };

        for (auto& elem : target_field->list_elements) {
            remap_element(elem);
        }
        fprintf(stderr, "Type indices remapped: %d, missing in target schema: %d\n",
            remapped, missing_types);
        // Verify clear worked
        if (!target_field->list_elements.empty()) {
            fprintf(stderr, "[AFTER REMAP] elem[0].cf=%zu cpo=0x%X\n",
                target_field->list_elements[0].child_fields.size(),
                target_field->list_elements[0].child_payload_offset);
        }

        // Verify elements have data
        size_t total_rv = 0;
        for (size_t i = 0; i < target_field->list_elements.size(); i++) {
            auto& e = target_field->list_elements[i];
            total_rv += e.raw_value.size();
            if (i < 5 || e.raw_value.size() > 1000) {
                fprintf(stderr, "  elem[%zu] rv=%zu cf=%zu cpo=0x%X start=0x%X\n",
                    i, e.raw_value.size(), e.child_fields.size(),
                    e.child_payload_offset, e.start_offset);
            }
        }
        fprintf(stderr, "Total element raw bytes: %zu\n", total_rv);

        // Direct blob replacement: splice donor raw bytes at the field's position.
        // This is the correct approach for cross-save transplant — the tree serializer
        // is designed for same-blob modifications where orig_blob has the gap data.
        {
            uint32_t field_start = orig_field_start;
            uint32_t field_end = orig_field_end;
            uint32_t old_size = field_end - field_start;
            if (old_size == 0) {
                fprintf(stderr, "ERROR: field has zero size — cannot splice\n");
                return 1;
            }

            // Build the new field bytes: list_header + all elements' raw_value
            std::vector<uint8_t> new_field_bytes;
            new_field_bytes.reserve(700000);
            // Header from donor
            new_field_bytes.insert(new_field_bytes.end(),
                donor_field->list_header_raw.begin(), donor_field->list_header_raw.end());
            // Elements (type indices already remapped in raw_value)
            for (auto& elem : target_field->list_elements) {
                new_field_bytes.insert(new_field_bytes.end(),
                    elem.raw_value.begin(), elem.raw_value.end());
            }

            int32_t delta = (int32_t)new_field_bytes.size() - (int32_t)old_size;
            fprintf(stderr, "Field splice: [0x%X..0x%X] old=%u new=%zu delta=%d\n",
                field_start, field_end, old_size, new_field_bytes.size(), delta);

            // Replace bytes in blob
            target.blob.erase(target.blob.begin() + field_start, target.blob.begin() + field_end);
            target.blob.insert(target.blob.begin() + field_start,
                new_field_bytes.begin(), new_field_bytes.end());

            // Fix TOC: find the block containing this field, update its data_size
            uint32_t toc_start = target.parsed.schema.schema_end + 12;
            for (size_t i = 0; i < target.parsed.toc.entries.size(); i++) {
                uint32_t ep = toc_start + (uint32_t)(i * 20);
                uint32_t doff = 0, dsz = 0;
                memcpy(&doff, target.blob.data() + ep + 12, 4);
                memcpy(&dsz, target.blob.data() + ep + 16, 4);
                if (field_start >= doff && field_start < doff + dsz) {
                    dsz = (uint32_t)((int32_t)dsz + delta);
                    memcpy(target.blob.data() + ep + 16, &dsz, 4);
                } else if (doff > field_start) {
                    doff = (uint32_t)((int32_t)doff + delta);
                    memcpy(target.blob.data() + ep + 12, &doff, 4);
                }
            }
            // Fix stream_size
            uint32_t ss = (uint32_t)target.blob.size();
            memcpy(target.blob.data() + target.parsed.schema.schema_end + 8, &ss, 4);

            // Fix POs after the splice point (same delta-based approach)
            static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
            uint32_t scan_start = field_start + (uint32_t)new_field_bytes.size();
            int po_fixed = 0;
            for (uint32_t p = scan_start; p + 12 <= (uint32_t)target.blob.size(); ++p) {
                if (target.blob[p] != 0xFF) continue;
                if (memcmp(target.blob.data() + p, SENT, 8) != 0) continue;
                uint32_t po_pos = p + 8;
                uint32_t cur_po = 0;
                memcpy(&cur_po, target.blob.data() + po_pos, 4);
                uint32_t correct = po_pos + 4;
                if (cur_po == correct) continue;
                if ((int32_t)cur_po + delta == (int32_t)correct) {
                    memcpy(target.blob.data() + po_pos, &correct, 4);
                    po_fixed++;
                }
            }
            // Fix POs within the new field bytes (make self-referential)
            for (uint32_t p = field_start; p + 12 <= field_start + (uint32_t)new_field_bytes.size(); ++p) {
                if (target.blob[p] != 0xFF) continue;
                if (memcmp(target.blob.data() + p, SENT, 8) != 0) continue;
                uint32_t po_pos = p + 8;
                uint32_t cur_po = 0;
                memcpy(&cur_po, target.blob.data() + po_pos, 4);
                uint32_t correct = po_pos + 4;
                if (cur_po != correct) {
                    memcpy(target.blob.data() + po_pos, &correct, 4);
                    po_fixed++;
                }
            }
            fprintf(stderr, "POs fixed: %d\n", po_fixed);
        }

        Reparse(target, false);
        fprintf(stderr, "New blob size: %zu bytes\n", target.blob.size());

        // Validate
        for (auto& obj : target.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name == field_path) {
                    fprintf(stderr, "After reload: %s has %zu elements\n",
                        field_path, f.list_elements.size());
                }
            }
            break;
        }

        WriteSave(target, output);
        fprintf(stderr, "Written: %s\n", output);
        return 0;
    }

    // ── NESTINSERT ── nested list insertion test (e.g., items)
    if (strcmp(cmd, "nestinsert") == 0) {
        if (argc < 5) {
            fprintf(stderr, "Usage: nestinsert <save> <block> <path> [-o out.save]\n"
                "  path = dotted path like _inventorylist[1]._itemList\n");
            return 1;
        }
        const char* block_class = argv[3];
        const char* path = argv[4];
        const char* output = save_path;
        for (int i = 5; i < argc; ++i) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[++i];
        }

        auto tree = LoadSave(save_path);
        fprintf(stderr, "Loaded. Finding last element in %s.%s to duplicate...\n", block_class, path);

        // Navigate to find the target list and get the last element's raw_value
        std::vector<uint8_t> dup_bytes;
        // Use InsertNested logic to find the list
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            // Simple path navigation for test
            auto* fields = &obj.fields;
            std::string remaining = path;
            GenericFieldValue* target = nullptr;
            while (!remaining.empty()) {
                size_t dot = remaining.find('.');
                size_t bracket = remaining.find('[');
                size_t seg_end = std::min({dot, bracket, remaining.size()});
                std::string fname = remaining.substr(0, seg_end);
                remaining = (seg_end < remaining.size()) ? remaining.substr(seg_end) : "";

                for (auto& f : *fields) {
                    if (f.name == fname) { target = &f; break; }
                }
                if (!target) break;

                if (!remaining.empty() && remaining[0] == '[') {
                    size_t close = remaining.find(']');
                    int idx = atoi(remaining.substr(1, close-1).c_str());
                    remaining = (close+1 < remaining.size()) ? remaining.substr(close+1) : "";
                    if (remaining.size() > 0 && remaining[0] == '.') remaining = remaining.substr(1);
                    if (idx < (int)target->list_elements.size()) {
                        fields = &target->list_elements[idx].child_fields;
                        target = nullptr;
                    }
                } else if (!remaining.empty() && remaining[0] == '.') {
                    remaining = remaining.substr(1);
                    fields = &target->child_fields;
                    target = nullptr;
                }
            }
            if (target && !target->list_elements.empty()) {
                dup_bytes = target->list_elements.back().raw_value;
                fprintf(stderr, "Found target list: %zu elements, duplicating last (%zu bytes)\n",
                    target->list_elements.size(), dup_bytes.size());
            }
            break;
        }
        if (dup_bytes.empty()) {
            fprintf(stderr, "ERROR: Could not find or list is empty at %s.%s\n", block_class, path);
            return 1;
        }

        auto result = InsertNested(tree, block_class, path, dup_bytes, -1);
        if (!result.ok) {
            fprintf(stderr, "ERROR: %s\n", result.error.c_str());
            return 1;
        }
        fprintf(stderr, "Inserted at index %d, growth=%d\n", result.new_element_index, result.growth);
        fprintf(stderr, "Blob size: %zu bytes\n", tree.blob.size());

        WriteSave(tree, output);
        fprintf(stderr, "Written: %s\n", output);

        // Validate
        auto tree2 = LoadSave(output);
        for (auto& obj : tree2.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            // Navigate again to check count
            auto* fields2 = &obj.fields;
            std::string rem2 = path;
            GenericFieldValue* t2 = nullptr;
            while (!rem2.empty()) {
                size_t dot = rem2.find('.'); size_t bracket = rem2.find('[');
                size_t se = std::min({dot, bracket, rem2.size()});
                std::string fn = rem2.substr(0, se);
                rem2 = (se < rem2.size()) ? rem2.substr(se) : "";
                for (auto& f : *fields2) { if (f.name == fn) { t2 = &f; break; } }
                if (!t2) break;
                if (!rem2.empty() && rem2[0] == '[') {
                    size_t c = rem2.find(']');
                    int idx = atoi(rem2.substr(1,c-1).c_str());
                    rem2 = (c+1<rem2.size())?rem2.substr(c+1):"";
                    if(!rem2.empty()&&rem2[0]=='.')rem2=rem2.substr(1);
                    if(idx<(int)t2->list_elements.size())fields2=&t2->list_elements[idx].child_fields;
                    t2=nullptr;
                } else if(!rem2.empty()&&rem2[0]=='.'){rem2=rem2.substr(1);fields2=&t2->child_fields;t2=nullptr;}
            }
            if (t2) fprintf(stderr, "After reload: %zu elements\n", t2->list_elements.size());
            break;
        }
        return 0;
    }

    // ── DUPINSERT ── (tree-based duplicate insertion test)
    if (strcmp(cmd, "dupinsert") == 0) {
        if (argc < 5) {
            fprintf(stderr, "Usage: dupinsert <save> <block> <field> [-o out.save]\n");
            return 1;
        }
        const char* block_class = argv[3];
        const char* list_field = argv[4];
        const char* output = save_path;
        for (int i = 5; i < argc; ++i) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[++i];
        }

        auto tree = LoadSave(save_path);
        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Loaded in %.1f ms\n",
            std::chrono::duration<double, std::milli>(t1 - t0).count());

        // Find last element's raw_value and duplicate it via InsertIntoList
        std::vector<uint8_t> dup_bytes;
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name == list_field && !f.list_elements.empty()) {
                    auto& last = f.list_elements.back();
                    dup_bytes = last.raw_value;
                    fprintf(stderr, "Duplicating last element: %zu bytes, type=%s\n",
                        dup_bytes.size(), last.child_type_name.c_str());
                }
            }
            break;
        }
        if (dup_bytes.empty()) {
            fprintf(stderr, "ERROR: Could not find list %s.%s or no elements\n",
                block_class, list_field);
            return 1;
        }

        auto result = InsertIntoList(tree, block_class, list_field, dup_bytes, -1);
        auto t2 = std::chrono::high_resolution_clock::now();
        if (!result.ok) {
            fprintf(stderr, "ERROR: %s\n", result.error.c_str());
            return 1;
        }
        fprintf(stderr, "Inserted at index %d, growth=%d (%.1f ms)\n",
            result.new_element_index, result.growth,
            std::chrono::duration<double, std::milli>(t2 - t1).count());

        // Validate the result
        fprintf(stderr, "Blob size: %zu bytes\n", tree.blob.size());

        WriteSave(tree, output);
        fprintf(stderr, "Written: %s\n", output);

        // Validate
        auto tree2 = LoadSave(output);
        fprintf(stderr, "Reparse OK: %zu objects\n", tree2.parsed.objects.size());
        for (auto& obj : tree2.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            for (auto& f : obj.fields) {
                if (f.name == list_field) {
                    fprintf(stderr, "List %s now has %zu elements (was %d)\n",
                        list_field, f.list_elements.size(),
                        result.new_element_index);
                }
            }
            break;
        }
        return 0;
    }

    // ── DYE-SCAN ──
    // Scan for dyed equipment items. Tests the dye detection fix.
    if (strcmp(cmd, "dye-scan") == 0) {
        auto tree = LoadSave(save_path);
        fprintf(stderr, "Scanning EquipmentSaveData for dye data...\n");

        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "EquipmentSaveData") continue;
            for (auto& fld : obj.fields) {
                if (fld.name != "_list" || fld.list_elements.empty()) continue;

                for (int ei = 0; ei < (int)fld.list_elements.size(); ei++) {
                    auto& equip = fld.list_elements[ei];
                    for (auto& cf : equip.child_fields) {
                        if (cf.name != "_item" || cf.child_fields.empty()) continue;

                        uint32_t itemKey = 0;
                        for (auto& icf : cf.child_fields) {
                            if (icf.name == "_itemKey" && icf.present) {
                                memcpy(&itemKey, tree.blob.data() + icf.start_offset, 4);
                            }
                            if (icf.name == "_itemDyeDataList" && icf.present) {
                                fprintf(stderr, "  equip[%d] itemKey=%u: _itemDyeDataList present=%d elems=%zu rv=%zu\n",
                                    ei, itemKey, icf.present ? 1 : 0,
                                    icf.list_elements.size(), icf.raw_value.size());
                                for (size_t di = 0; di < icf.list_elements.size(); di++) {
                                    auto& de = icf.list_elements[di];
                                    fprintf(stderr, "    dye[%zu] off=[0x%X..0x%X] cf=%zu ti=%d\n",
                                        di, de.start_offset, de.end_offset,
                                        de.child_fields.size(), de.child_type_index);
                                }
                            }
                            if (icf.name == "_socketSaveDataList" && icf.present) {
                                fprintf(stderr, "  equip[%d] itemKey=%u: _socketSaveDataList present=%d elems=%zu rv=%zu\n",
                                    ei, itemKey, icf.present ? 1 : 0,
                                    icf.list_elements.size(), icf.raw_value.size());
                            }
                        }
                    }
                }
            }
            break;
        }
        return 0;
    }

    // ── NESTED-INSERT-TEST ──
    // Tests nested insertion + roundtrip. Copies an existing element and reinserts it.
    // Usage: nested-insert-test <save> <block> <path> [-o output.save]
    // Example: nested-insert-test save.save EquipmentSaveData "_list[0]._item._socketSaveDataList"
    if (strcmp(cmd, "nested-insert-test") == 0) {
        if (argc < 5) {
            fprintf(stderr, "Usage: nested-insert-test <save> <block> <path> [-o output]\n");
            fprintf(stderr, "Copies element[0] from the path's list and reinserts it.\n");
            return 1;
        }
        const char* block_class = argv[3];
        const char* nested_path = argv[4];
        const char* output = nullptr;
        for (int i = 5; i < argc; i++) {
            if (strcmp(argv[i], "-o") == 0 && i + 1 < argc) output = argv[i + 1];
        }

        auto tree = LoadSave(save_path);
        auto t1 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Loaded: %zu bytes, %zu types\n", tree.blob.size(), tree.parsed.schema.types.size());

        // Find the target list and grab element[0]'s bytes
        auto segments = std::string(nested_path);
        // Navigate to the list to find an existing element to clone
        fprintf(stderr, "Navigating: %s.%s\n", block_class, nested_path);

        // Find the list field by following the path
        std::vector<GenericFieldValue>* cur = nullptr;
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            cur = &obj.fields;
            break;
        }
        if (!cur) { fprintf(stderr, "Block not found: %s\n", block_class); return 1; }

        // Simple path navigation to find the final list
        GenericFieldValue* target = nullptr;
        {
            // Parse the path manually
            std::string p = nested_path;
            size_t pos = 0;
            while (pos < p.size()) {
                if (p[pos] == '.') { pos++; continue; }
                size_t bracket = p.find('[', pos);
                size_t dot = p.find('.', pos);
                size_t end = std::min(bracket, dot);
                if (end == std::string::npos) end = p.size();
                std::string fname = p.substr(pos, end - pos);
                pos = end;
                int idx = -1;
                if (pos < p.size() && p[pos] == '[') {
                    size_t close = p.find(']', pos);
                    if (close != std::string::npos) {
                        idx = atoi(p.substr(pos + 1, close - pos - 1).c_str());
                        pos = close + 1;
                    }
                }
                // Find field
                GenericFieldValue* found = nullptr;
                for (auto& f : *cur) {
                    if (f.name == fname) { found = &f; break; }
                }
                if (!found) { fprintf(stderr, "Field not found: %s\n", fname.c_str()); return 1; }
                target = found;
                if (idx >= 0 && idx < (int)found->list_elements.size()) {
                    cur = &found->list_elements[idx].child_fields;
                } else if (!found->child_fields.empty()) {
                    cur = &found->child_fields;
                }
            }
        }

        if (!target || target->list_elements.empty()) {
            fprintf(stderr, "Target list empty or not found\n");
            return 1;
        }

        // Clone element[0]
        auto& elem0 = target->list_elements[0];
        std::vector<uint8_t> clone_bytes;
        if (!elem0.raw_value.empty()) {
            clone_bytes = elem0.raw_value;
        } else if (elem0.end_offset > elem0.start_offset) {
            clone_bytes.assign(tree.blob.begin() + elem0.start_offset,
                               tree.blob.begin() + elem0.end_offset);
        }

        fprintf(stderr, "Cloning element[0]: %zu bytes, type=%s\n",
            clone_bytes.size(), elem0.child_type_name.c_str());
        fprintf(stderr, "List currently has %zu elements\n", target->list_elements.size());

        size_t old_blob = tree.blob.size();
        size_t old_elems = target->list_elements.size();

        // Insert via InsertNested
        auto result = InsertNested(tree, block_class, nested_path, clone_bytes);
        if (!result.ok) {
            fprintf(stderr, "InsertNested FAILED: %s\n", result.error.c_str());
            return 1;
        }

        fprintf(stderr, "InsertNested OK: growth=%d, new_index=%d\n", result.growth, result.new_element_index);
        fprintf(stderr, "Blob: %zu -> %zu (delta=%d)\n", old_blob, tree.blob.size(),
            (int)tree.blob.size() - (int)old_blob);

        // Re-find the list to check element count
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name.find(block_class) == std::string::npos) continue;
            cur = &obj.fields;
            GenericFieldValue* t2 = nullptr;
            {
                std::string p2 = nested_path;
                size_t pos2 = 0;
                while (pos2 < p2.size()) {
                    if (p2[pos2] == '.') { pos2++; continue; }
                    size_t bracket = p2.find('[', pos2);
                    size_t dot = p2.find('.', pos2);
                    size_t end2 = std::min(bracket, dot);
                    if (end2 == std::string::npos) end2 = p2.size();
                    std::string fname = p2.substr(pos2, end2 - pos2);
                    pos2 = end2;
                    int idx = -1;
                    if (pos2 < p2.size() && p2[pos2] == '[') {
                        size_t close = p2.find(']', pos2);
                        if (close != std::string::npos) {
                            idx = atoi(p2.substr(pos2 + 1, close - pos2 - 1).c_str());
                            pos2 = close + 1;
                        }
                    }
                    GenericFieldValue* found = nullptr;
                    for (auto& f : *cur) { if (f.name == fname) { found = &f; break; } }
                    if (!found) break;
                    t2 = found;
                    if (idx >= 0 && idx < (int)found->list_elements.size())
                        cur = &found->list_elements[idx].child_fields;
                    else if (!found->child_fields.empty())
                        cur = &found->child_fields;
                }
            }
            if (t2) fprintf(stderr, "After insert: %zu elements (was %zu)\n", t2->list_elements.size(), old_elems);
            break;
        }

        // Roundtrip verify
        {
            auto fresh = ParcSerializer::Serialize(tree.parsed, tree.blob);
            if (fresh.size() != tree.blob.size()) {
                fprintf(stderr, "ROUNDTRIP SIZE MISMATCH: %zu vs %zu\n", tree.blob.size(), fresh.size());
            } else {
                int diffs = 0;
                for (size_t i = 0; i < fresh.size(); i++)
                    if (fresh[i] != tree.blob[i]) diffs++;
                fprintf(stderr, "ROUNDTRIP: %s (%d diffs)\n", diffs == 0 ? "PERFECT MATCH" : "DIFFS", diffs);
            }
        }

        // Validate POs
        {
            static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
            int valid = 0, broken_po = 0;
            for (uint32_t p = tree.parsed.schema.schema_end; p + 12 <= (uint32_t)tree.blob.size(); p++) {
                if (memcmp(tree.blob.data() + p, SENT, 8) != 0) continue;
                uint32_t toc_start = tree.parsed.schema.schema_end + 12;
                uint32_t toc_end = toc_start + (uint32_t)tree.parsed.toc.entries.size() * 20;
                if (p >= toc_start && p < toc_end) continue;
                uint32_t po_pos = p + 8;
                uint32_t po_val = 0;
                memcpy(&po_val, tree.blob.data() + po_pos, 4);
                if (po_val == po_pos + 4) valid++;
                else broken_po++;
            }
            fprintf(stderr, "PO CHECK: %d valid, %d broken\n", valid, broken_po);
        }

        if (output) {
            WriteSave(tree, output);
            fprintf(stderr, "Written: %s\n", output);
        }

        auto t2 = std::chrono::high_resolution_clock::now();
        fprintf(stderr, "Done (%.1f ms)\n", std::chrono::duration<double, std::milli>(t2 - t1).count());
        return 0;
    }

    // ── ADD-ITEM ──
    // Build a fresh item via ItemFactory and insert into inventory bag.
    // Usage: add-item <save> <itemKey> [--bag N] [--count N] [--mask equipment|consumable|quest] [-o out]
    if (strcmp(cmd, "add-item") == 0) {
        if (argc < 4) {
            fprintf(stderr, "Usage: add-item <save> <itemKey> [--bag N] [--count N] [--slot N] [--mask equipment|consumable|quest] [-o output]\n");
            return 1;
        }
        uint32_t itemKey = (uint32_t)strtoul(argv[3], nullptr, 0);
        int bag = 0;
        uint64_t count = 1;
        uint16_t slot = 0;
        const char* output = nullptr;
        const char* maskStr = "consumable";

        for (int i = 4; i < argc; i++) {
            if (strcmp(argv[i], "--bag") == 0 && i+1 < argc) bag = atoi(argv[++i]);
            else if (strcmp(argv[i], "--count") == 0 && i+1 < argc) count = strtoull(argv[++i], nullptr, 0);
            else if (strcmp(argv[i], "--slot") == 0 && i+1 < argc) slot = (uint16_t)atoi(argv[++i]);
            else if (strcmp(argv[i], "--mask") == 0 && i+1 < argc) maskStr = argv[++i];
            else if (strcmp(argv[i], "-o") == 0 && i+1 < argc) output = argv[++i];
        }

        auto tree = LoadSave(save_path);
        fprintf(stderr, "Loaded: %zu bytes, %zu types, %zu objects\n",
            tree.blob.size(), tree.parsed.schema.types.size(), tree.parsed.objects.size());

        // Build type index map from target save
        std::unordered_map<std::string, uint16_t> ti_map;
        for (size_t i = 0; i < tree.parsed.schema.types.size(); i++)
            ti_map[tree.parsed.schema.types[i].name] = (uint16_t)i;

        // Parse mask type
        ItemFactory::ItemMask mask = ItemFactory::ItemMask::SimpleConsumable;
        if (strstr(maskStr, "equipment")) mask = ItemFactory::ItemMask::Equipment;
        else if (strstr(maskStr, "equip_nosock")) mask = ItemFactory::ItemMask::EquipmentNoSocket;
        else if (strstr(maskStr, "quest")) mask = ItemFactory::ItemMask::QuestItem;
        else if (strstr(maskStr, "consum")) mask = ItemFactory::ItemMask::SimpleConsumable;

        // Generate unique itemNo from timestamp
        uint64_t itemNo = ((uint64_t)itemKey << 32) | (uint64_t)(tree.blob.size() & 0xFFFFFFFF);

        ItemFactory::ItemSpec spec;
        spec.itemKey = itemKey;
        spec.itemNo = itemNo;
        spec.slotNo = slot;
        spec.stackCount = count;
        spec.maskType = mask;
        spec.endurance = 65535;
        spec.isNewMark = true;

        fprintf(stderr, "Building item: key=%u no=%llu count=%llu mask=%s bag=%d slot=%d\n",
            itemKey, (unsigned long long)itemNo, (unsigned long long)count, maskStr, bag, slot);

        auto item_bytes = ItemFactory::BuildItem(spec, ti_map);
        if (item_bytes.empty()) {
            fprintf(stderr, "FAILED: ItemFactory::BuildItem returned empty (missing ItemSaveData in schema?)\n");
            return 1;
        }
        fprintf(stderr, "Built: %zu bytes\n", item_bytes.size());

        // Log hex of first 32 bytes
        fprintf(stderr, "Hex: ");
        for (size_t i = 0; i < std::min<size_t>(32, item_bytes.size()); i++)
            fprintf(stderr, "%02X ", item_bytes[i]);
        fprintf(stderr, "\n");

        // Check inventory structure
        int max_bag = -1;
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "InventorySaveData") continue;
            for (auto& f : obj.fields) {
                if (f.name == "_inventorylist") {
                    max_bag = (int)f.list_elements.size() - 1;
                    fprintf(stderr, "Inventory has %zu bags (0..%d)\n", f.list_elements.size(), max_bag);
                    if (bag <= max_bag) {
                        auto& bag_elem = f.list_elements[bag];
                        for (auto& cf : bag_elem.child_fields) {
                            if (cf.name == "_itemList") {
                                fprintf(stderr, "Bag[%d]._itemList: %zu items currently\n", bag, cf.list_elements.size());
                            }
                        }
                    }
                }
            }
            break;
        }

        if (bag > max_bag) {
            fprintf(stderr, "FAILED: bag %d out of range (max=%d)\n", bag, max_bag);
            return 1;
        }

        // Build nested path: _inventorylist[bag]._itemList
        char path[128];
        snprintf(path, sizeof(path), "_inventorylist[%d]._itemList", bag);
        fprintf(stderr, "Insert path: InventorySaveData.%s\n", path);

        size_t old_blob = tree.blob.size();
        auto result = InsertNested(tree, "InventorySaveData", path, item_bytes);
        if (!result.ok) {
            fprintf(stderr, "FAILED: InsertNested: %s\n", result.error.c_str());
            return 1;
        }

        fprintf(stderr, "InsertNested OK: growth=%d new_index=%d\n", result.growth, result.new_element_index);
        fprintf(stderr, "Blob: %zu -> %zu\n", old_blob, tree.blob.size());

        // Verify the item landed
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "InventorySaveData") continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist") continue;
                if (bag < (int)f.list_elements.size()) {
                    for (auto& cf : f.list_elements[bag].child_fields) {
                        if (cf.name == "_itemList")
                            fprintf(stderr, "Bag[%d]._itemList after: %zu items\n", bag, cf.list_elements.size());
                    }
                }
            }
            break;
        }

        // Roundtrip
        {
            auto fresh = ParcSerializer::Serialize(tree.parsed, tree.blob);
            if (fresh.size() != tree.blob.size())
                fprintf(stderr, "ROUNDTRIP SIZE MISMATCH: %zu vs %zu\n", tree.blob.size(), fresh.size());
            else {
                int d = 0;
                for (size_t i = 0; i < fresh.size(); i++) if (fresh[i] != tree.blob[i]) d++;
                fprintf(stderr, "ROUNDTRIP: %s (%d diffs)\n", d == 0 ? "PERFECT MATCH" : "DIFFS", d);
            }
        }

        // Validate
        {
            static const uint8_t SENT[8] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
            int valid = 0, broken_po = 0;
            for (uint32_t p = tree.parsed.schema.schema_end; p + 12 <= (uint32_t)tree.blob.size(); p++) {
                if (memcmp(tree.blob.data() + p, SENT, 8) != 0) continue;
                uint32_t toc_start = tree.parsed.schema.schema_end + 12;
                uint32_t toc_end = toc_start + (uint32_t)tree.parsed.toc.entries.size() * 20;
                if (p >= toc_start && p < toc_end) continue;
                uint32_t po_pos = p + 8;
                uint32_t po_val = 0; memcpy(&po_val, tree.blob.data() + po_pos, 4);
                if (po_val == po_pos + 4) valid++; else broken_po++;
            }
            fprintf(stderr, "PO CHECK: %d valid, %d broken\n", valid, broken_po);
        }

        // Check FactionSpawn survived
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "FactionSpawnStageManagerSaveData") continue;
            int elems = 0;
            for (auto& f : obj.fields) elems += (int)f.list_elements.size();
            fprintf(stderr, "FactionSpawn: %d elements (must be >0)\n", elems);
            break;
        }

        if (output) {
            WriteSave(tree, output);
            fprintf(stderr, "Written: %s (%zu bytes)\n", output,
                std::filesystem::file_size(output));
        }

        fprintf(stderr, "Done.\n");
        return 0;
    }

    // ── DYE-INJECT ──
    // Inject _itemDyeDataList into an equipment item that doesn't have dye.
    // Usage: dye-inject <save> --equip <index> --slots <N> --rgb <R,G,B> [-o out]
    if (strcmp(cmd, "dye-inject") == 0) {
        if (argc < 6) {
            fprintf(stderr, "Usage: dye-inject <save> --equip <idx> --slots <N> --rgb <R,G,B> [--material M] [--group G] [-o out]\n");
            return 1;
        }
        int equip_idx = 0;
        int num_slots = 1;
        uint8_t dr = 255, dg = 0, db = 0, da = 255;
        uint16_t material = 0;
        uint32_t color_group = 0;
        const char* output = nullptr;

        for (int i = 3; i < argc; i++) {
            if (strcmp(argv[i], "--equip") == 0 && i+1 < argc) equip_idx = atoi(argv[++i]);
            else if (strcmp(argv[i], "--slots") == 0 && i+1 < argc) num_slots = atoi(argv[++i]);
            else if (strcmp(argv[i], "--rgb") == 0 && i+1 < argc) {
                sscanf(argv[++i], "%hhu,%hhu,%hhu", &dr, &dg, &db);
            }
            else if (strcmp(argv[i], "--material") == 0 && i+1 < argc) material = (uint16_t)atoi(argv[++i]);
            else if (strcmp(argv[i], "--group") == 0 && i+1 < argc) color_group = (uint32_t)strtoul(argv[++i], nullptr, 0);
            else if (strcmp(argv[i], "-o") == 0 && i+1 < argc) output = argv[++i];
        }

        auto tree = LoadSave(save_path);
        fprintf(stderr, "Loaded: %zu bytes, %zu types\n", tree.blob.size(), tree.parsed.schema.types.size());

        // Find ItemDyeSaveData type index
        auto dye_ti_it = tree.name_to_type_idx.find("ItemDyeSaveData");
        if (dye_ti_it == tree.name_to_type_idx.end()) {
            fprintf(stderr, "FAILED: ItemDyeSaveData not in schema. Dye an item once in-game first.\n");
            return 1;
        }
        uint16_t dye_ti = (uint16_t)dye_ti_it->second;
        fprintf(stderr, "ItemDyeSaveData type_index=%u\n", dye_ti);

        // Find the equipment item
        GenericFieldValue* target_item = nullptr;
        uint32_t item_start = 0, item_end = 0;
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "EquipmentSaveData") continue;
            for (auto& fld : obj.fields) {
                if (fld.name != "_list" || equip_idx >= (int)fld.list_elements.size()) continue;
                auto& equip = fld.list_elements[equip_idx];
                for (auto& cf : equip.child_fields) {
                    if (cf.name == "_item") {
                        target_item = &cf;
                        // Find actual item element inside _item
                        item_start = cf.start_offset;
                        item_end = cf.end_offset;
                        break;
                    }
                }
            }
            break;
        }
        if (!target_item) { fprintf(stderr, "FAILED: equip[%d]._item not found\n", equip_idx); return 1; }

        // Check current state
        uint32_t itemKey = 0;
        bool has_dye = false;
        uint32_t dye_insert_pos = 0; // relative offset in item bytes where dye list should go
        for (auto& icf : target_item->child_fields) {
            if (icf.name == "_itemKey" && icf.present) {
                memcpy(&itemKey, tree.blob.data() + icf.start_offset, 4);
            }
            if (icf.name == "_itemDyeDataList" && icf.present) has_dye = true;
            // _itemDyeDataList goes after _socketSaveDataList (bit 13) and before _dropResultSubSaveItemList (bit 15)
            if (icf.name == "_socketSaveDataList" && icf.present) {
                dye_insert_pos = icf.end_offset - item_start;
            }
            if (icf.name == "_dropResultSubSaveItemList" && icf.present && dye_insert_pos == 0) {
                dye_insert_pos = icf.start_offset - item_start;
            }
            if (icf.name == "_transferredItemKey" && icf.present && dye_insert_pos == 0) {
                dye_insert_pos = icf.start_offset - item_start;
            }
        }

        fprintf(stderr, "Target: equip[%d] itemKey=%u, has_dye=%s\n",
            equip_idx, itemKey, has_dye ? "YES" : "NO");

        if (has_dye) {
            fprintf(stderr, "Item already has dye data. Use the Dye tab to edit colors.\n");
            return 1;
        }
        if (dye_insert_pos == 0) {
            fprintf(stderr, "FAILED: cannot determine where to insert dye list\n");
            return 1;
        }

        // Extract current item bytes
        std::vector<uint8_t> item_bytes;
        if (!target_item->raw_value.empty()) {
            item_bytes = target_item->raw_value;
        } else {
            item_bytes.assign(tree.blob.begin() + item_start, tree.blob.begin() + item_end);
        }
        fprintf(stderr, "Item bytes: %zu, dye_insert_pos=+0x%X\n", item_bytes.size(), dye_insert_pos);

        // Step 1: Set bit 14 in the mask
        // Mask starts at offset 2 (after mbc u16)
        uint16_t mbc = item_bytes[0] | (item_bytes[1] << 8);
        if (mbc < 2 || 2 + mbc > item_bytes.size()) {
            fprintf(stderr, "FAILED: bad mbc=%u\n", mbc);
            return 1;
        }
        // Bit 14 is in mask byte index 1 (bits 8-15), bit 6 of that byte
        if (mbc >= 2) {
            item_bytes[2 + 1] |= (1 << 6); // bit 14 = byte[1] bit 6
            fprintf(stderr, "Set mask bit 14 (_itemDyeDataList)\n");
        }

        // Step 2: Build dye list bytes
        // List header (18 bytes): prefix=0, count=LE_u24, reserved×3(u32), reserved(u16)
        std::vector<uint8_t> dye_list;
        auto push8 = [&](uint8_t v) { dye_list.push_back(v); };
        auto push16 = [&](uint16_t v) { dye_list.push_back(v&0xFF); dye_list.push_back((v>>8)&0xFF); };
        auto push32 = [&](uint32_t v) { for(int i=0;i<4;i++) dye_list.push_back((v>>(i*8))&0xFF); };

        push8(0); // prefix
        push8(num_slots & 0xFF); push8((num_slots>>8)&0xFF); push8((num_slots>>16)&0xFF); // count u24
        push32(0); push32(0); push32(0); // reserved ×3
        push16(0); // reserved u16

        // Each dye slot element
        for (int s = 0; s < num_slots; s++) {
            uint32_t elem_start_off = (uint32_t)dye_list.size();
            // Header: mbc=1, mask=0xFF (all 8 fields), type_index, reserved, sentinel, PO
            push16(1); // mbc
            push8(0xFF); // mask = all 8 fields
            push16(dye_ti); // type_index
            push8(0); // reserved
            for (int i = 0; i < 8; i++) push8(0xFF); // sentinel
            uint32_t po_off = (uint32_t)dye_list.size();
            push32(0); // PO placeholder
            uint32_t payload_start = (uint32_t)dye_list.size();
            // Patch PO to self-ref (will be fixed by ReplaceElement later)
            uint32_t po_val = payload_start;
            memcpy(dye_list.data() + po_off, &po_val, 4);
            push32(0); // reserved_u32
            // Fields (all 8, in order):
            push8((int8_t)s);   // _dyeSlotNo
            push8(dr);          // _dyeColorR
            push8(dg);          // _dyeColorG
            push8(db);          // _dyeColorB
            push8(da);          // _dyeColorA
            push8(0);           // _grimeOpacity
            push32(color_group); // _dyeColorGroupInfoKey
            push16(material);   // _texturePalleteKey
            // Trailing size
            uint32_t ts = (uint32_t)dye_list.size() - payload_start;
            push32(ts);
        }

        fprintf(stderr, "Built dye list: %zu bytes (%d slots, RGB=%u,%u,%u)\n",
            dye_list.size(), num_slots, dr, dg, db);

        // Step 3: Insert dye list into item bytes at the correct position
        item_bytes.insert(item_bytes.begin() + dye_insert_pos,
                          dye_list.begin(), dye_list.end());

        // Step 4: Fix trailing_size at the end of the item
        // Trailing size is last 4 bytes, value = distance from payload_start to trailing_size
        uint32_t old_ts = 0;
        memcpy(&old_ts, item_bytes.data() + item_bytes.size() - 4, 4);
        uint32_t new_ts = old_ts + (uint32_t)dye_list.size();
        memcpy(item_bytes.data() + item_bytes.size() - 4, &new_ts, 4);
        fprintf(stderr, "Fixed item trailing_size: %u -> %u\n", old_ts, new_ts);

        // Step 5: Use ReplaceElement to swap old item with new (larger) item
        fprintf(stderr, "Replacing element at [0x%X..0x%X] with %zu bytes (growth=%zu)\n",
            item_start, item_end, item_bytes.size(), item_bytes.size() - (item_end - item_start));

        auto result = ReplaceElement(tree, "EquipmentSaveData",
            item_start, item_end, item_bytes);
        if (!result.ok) {
            fprintf(stderr, "FAILED: ReplaceElement: %s\n", result.error.c_str());
            return 1;
        }
        fprintf(stderr, "ReplaceElement OK: growth=%d, POs fixed=%d\n", result.growth, result.po_fixed);

        // Verify
        {
            auto fresh = ParcSerializer::Serialize(tree.parsed, tree.blob);
            if (fresh.size() != tree.blob.size())
                fprintf(stderr, "ROUNDTRIP SIZE MISMATCH: %zu vs %zu\n", tree.blob.size(), fresh.size());
            else {
                int d = 0;
                for (size_t i = 0; i < fresh.size(); i++) if (fresh[i] != tree.blob[i]) d++;
                fprintf(stderr, "ROUNDTRIP: %s (%d diffs)\n", d == 0 ? "PERFECT MATCH" : "DIFFS", d);
            }
        }

        // Verify dye data is now present
        fprintf(stderr, "=== DYE SCAN AFTER INJECT ===\n");
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "EquipmentSaveData") continue;
            for (auto& fld : obj.fields) {
                if (fld.name != "_list") continue;
                if (equip_idx < (int)fld.list_elements.size()) {
                    for (auto& cf : fld.list_elements[equip_idx].child_fields) {
                        if (cf.name != "_item") continue;
                        for (auto& icf : cf.child_fields) {
                            if (icf.name == "_itemDyeDataList") {
                                fprintf(stderr, "  _itemDyeDataList: present=%d elems=%zu rv=%zu\n",
                                    icf.present?1:0, icf.list_elements.size(), icf.raw_value.size());
                            }
                        }
                    }
                }
            }
            break;
        }

        // FactionSpawn check
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "FactionSpawnStageManagerSaveData") continue;
            int e = 0; for (auto& f : obj.fields) e += (int)f.list_elements.size();
            fprintf(stderr, "FactionSpawn: %d elements\n", e);
            break;
        }

        if (output) {
            WriteSave(tree, output);
            fprintf(stderr, "Written: %s\n", output);
        }
        return 0;
    }

    // ── EXTRACT-TEMPLATES ──
    // Scan all items in the save, extract unique mask patterns as reusable templates.
    // Writes templates to a directory as .bin files, plus an index.json.
    if (strcmp(cmd, "extract-templates") == 0) {
        if (argc < 4) { fprintf(stderr, "Usage: extract-templates <save> <output_dir>\n"); return 1; }
        const char* out_dir = argv[3];
        std::filesystem::create_directories(out_dir);

        auto tree = LoadSave(save_path);
        fprintf(stderr, "Loaded: %zu bytes\n", tree.blob.size());

        struct Template {
            uint32_t mask_bits = 0;
            uint16_t mbc = 0;
            std::vector<uint8_t> bytes;
            uint32_t example_key = 0;
            int count = 0;
            // Field offsets for patching
            uint32_t off_key = 0, off_no = 0, off_count = 0, off_transfer = 0, off_slot = 0;
        };
        std::map<uint32_t, Template> templates; // mask_bits -> template

        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "InventorySaveData" && obj.class_name != "EquipmentSaveData") continue;

            std::function<void(const GenericFieldValue&)> scanList;
            scanList = [&](const GenericFieldValue& list_field) {
                for (auto& elem : list_field.list_elements) {
                    // Check if this is an ItemSaveData element
                    if (elem.child_type_name != "ItemSaveData") {
                        // Recurse into child lists
                        for (auto& cf : elem.child_fields) {
                            if ((cf.meta_kind == 6 || cf.meta_kind == 7) && !cf.list_elements.empty())
                                scanList(cf);
                        }
                        continue;
                    }

                    // Extract the mask bits
                    uint32_t mask = 0;
                    if (elem.child_mask_bytes.size() >= 1) mask |= elem.child_mask_bytes[0];
                    if (elem.child_mask_bytes.size() >= 2) mask |= (uint32_t)elem.child_mask_bytes[1] << 8;
                    if (elem.child_mask_bytes.size() >= 3) mask |= (uint32_t)elem.child_mask_bytes[2] << 16;
                    if (elem.child_mask_bytes.size() >= 4) mask |= (uint32_t)elem.child_mask_bytes[3] << 24;

                    auto& tmpl = templates[mask];
                    tmpl.count++;
                    if (!tmpl.bytes.empty()) return; // already have this pattern

                    tmpl.mask_bits = mask;
                    tmpl.mbc = elem.child_mask_byte_count;

                    // Extract raw bytes
                    if (!elem.raw_value.empty())
                        tmpl.bytes = elem.raw_value;
                    else if (elem.end_offset > elem.start_offset)
                        tmpl.bytes.assign(tree.blob.begin() + elem.start_offset,
                                          tree.blob.begin() + elem.end_offset);

                    // Find field offsets
                    uint32_t base = elem.start_offset;
                    for (auto& nf : elem.child_fields) {
                        if (!nf.present || nf.start_offset == 0) continue;
                        uint32_t rel = nf.start_offset - base;
                        if (nf.name == "_itemKey") { uint32_t v=0; memcpy(&v,tree.blob.data()+nf.start_offset,4); tmpl.off_key = rel; tmpl.example_key = v; }
                        if (nf.name == "_itemNo") tmpl.off_no = rel;
                        if (nf.name == "_stackCount") tmpl.off_count = rel;
                        if (nf.name == "_transferredItemKey") tmpl.off_transfer = rel;
                        if (nf.name == "_slotNo") tmpl.off_slot = rel;
                    }
                };
            };

            for (auto& f : obj.fields) {
                if ((f.meta_kind == 6 || f.meta_kind == 7) && !f.list_elements.empty())
                    scanList(f);
            }
        }

        fprintf(stderr, "Found %zu unique mask patterns\n", templates.size());

        // Write templates + index
        nlohmann::json idx_arr = nlohmann::json::array();
        int ti = 0;
        for (auto& kv : templates) {
            uint32_t mask = kv.first;
            auto& tmpl = kv.second;
            char fname[64];
            snprintf(fname, sizeof(fname), "template_%02d_0x%08X.bin", ti, mask);
            std::string fpath = std::string(out_dir) + "/" + fname;
            std::ofstream f(fpath, std::ios::binary);
            f.write((const char*)tmpl.bytes.data(), tmpl.bytes.size());

            // Decode which fields are present
            std::vector<std::string> fields;
            const char* field_names[] = {
                "_saveVersion","_itemNo","_itemKey","_slotNo","_stackCount",
                "_enchantLevel","_useableCtc","_endurance","_sharpness",
                "_batteryStat","_maxBatteryStat","_maxSocketCount","_validSocketCount",
                "_socketSaveDataList","_itemDyeDataList","_dropResultSubSaveItemList",
                "_transferredItemKey","_currentGimmickState","_maxChargeUseableCount",
                "_chargedUseableCount","_coolTimePerCharge","_timeWhenPushItem",
                "_characterConversionData","_isNewMark","_isLocked"
            };
            for (int b = 0; b < 25; b++)
                if (mask & (1u << b)) fields.push_back(field_names[b]);

            nlohmann::json entry;
            entry["file"] = fname;
            entry["mask"] = mask;
            entry["mask_hex"] = (std::ostringstream() << "0x" << std::hex << mask).str();
            entry["size"] = tmpl.bytes.size();
            entry["fields"] = fields;
            entry["example_key"] = tmpl.example_key;
            entry["count"] = tmpl.count;
            entry["patch_offsets"] = {
                {"_itemKey", tmpl.off_key},
                {"_itemNo", tmpl.off_no},
                {"_stackCount", tmpl.off_count},
                {"_transferredItemKey", tmpl.off_transfer},
                {"_slotNo", tmpl.off_slot}
            };
            idx_arr.push_back(entry);

            fprintf(stderr, "  [%d] mask=0x%08X size=%zuB count=%d example=%u fields=%zu %s\n",
                ti, mask, tmpl.bytes.size(), tmpl.count, tmpl.example_key, fields.size(), fname);
            ti++;
        }

        std::string idx_path = std::string(out_dir) + "/templates.json";
        std::ofstream idx_f(idx_path);
        idx_f << idx_arr.dump(2);
        fprintf(stderr, "Index: %s\n", idx_path.c_str());
        return 0;
    }

    // ── CLONE-ITEM ──
    // Clone an existing item, patch key/count/transferKey, reinsert.
    // This is the SAFE approach — preserves exact structure from a real game item.
    // Usage: clone-item <save> --source <bag.slot> --key <newItemKey> [--count N] [--bag N] [-o out]
    if (strcmp(cmd, "clone-item") == 0) {
        if (argc < 6) {
            fprintf(stderr, "Usage: clone-item <save> --source <bag.slot> --key <itemKey> [--count N] [--bag N] [-o out]\n");
            fprintf(stderr, "Example: clone-item save.save --source 0.0 --key 12218 --count 99 --bag 0\n");
            return 1;
        }
        int src_bag = 0, src_slot = 0;
        uint32_t new_key = 0;
        uint64_t new_count = 0; // 0 = keep original
        int dst_bag = -1; // -1 = same as source
        const char* output = nullptr;

        for (int i = 3; i < argc; i++) {
            if (strcmp(argv[i], "--source") == 0 && i+1 < argc) {
                sscanf(argv[++i], "%d.%d", &src_bag, &src_slot);
            } else if (strcmp(argv[i], "--key") == 0 && i+1 < argc) {
                new_key = (uint32_t)strtoul(argv[++i], nullptr, 0);
            } else if (strcmp(argv[i], "--count") == 0 && i+1 < argc) {
                new_count = strtoull(argv[++i], nullptr, 0);
            } else if (strcmp(argv[i], "--bag") == 0 && i+1 < argc) {
                dst_bag = atoi(argv[++i]);
            } else if (strcmp(argv[i], "-o") == 0 && i+1 < argc) {
                output = argv[++i];
            }
        }
        if (new_key == 0) { fprintf(stderr, "ERROR: --key required\n"); return 1; }
        if (dst_bag < 0) dst_bag = src_bag;

        auto tree = LoadSave(save_path);
        fprintf(stderr, "Loaded: %zu bytes, %zu types\n", tree.blob.size(), tree.parsed.schema.types.size());

        // Find source item
        std::vector<uint8_t> clone_bytes;
        uint32_t orig_key_offset = 0;
        uint32_t orig_no_offset = 0;
        uint32_t orig_count_offset = 0;
        uint32_t orig_transfer_offset = 0;

        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "InventorySaveData") continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist") continue;
                if (src_bag >= (int)f.list_elements.size()) {
                    fprintf(stderr, "ERROR: bag %d out of range (max %d)\n", src_bag, (int)f.list_elements.size()-1);
                    return 1;
                }
                auto& bag_elem = f.list_elements[src_bag];
                for (auto& cf : bag_elem.child_fields) {
                    if (cf.name != "_itemList") continue;
                    if (src_slot >= (int)cf.list_elements.size()) {
                        fprintf(stderr, "ERROR: slot %d out of range (max %d)\n", src_slot, (int)cf.list_elements.size()-1);
                        return 1;
                    }
                    auto& item = cf.list_elements[src_slot];
                    // Extract raw bytes
                    if (!item.raw_value.empty()) {
                        clone_bytes = item.raw_value;
                    } else if (item.end_offset > item.start_offset) {
                        clone_bytes.assign(tree.blob.begin() + item.start_offset,
                                           tree.blob.begin() + item.end_offset);
                    }
                    // Find field offsets for patching (relative to element start)
                    uint32_t elem_start = item.start_offset;
                    for (auto& nf : item.child_fields) {
                        if (nf.name == "_itemKey" && nf.present)
                            orig_key_offset = nf.start_offset - elem_start;
                        if (nf.name == "_itemNo" && nf.present)
                            orig_no_offset = nf.start_offset - elem_start;
                        if (nf.name == "_stackCount" && nf.present)
                            orig_count_offset = nf.start_offset - elem_start;
                        if (nf.name == "_transferredItemKey" && nf.present)
                            orig_transfer_offset = nf.start_offset - elem_start;
                    }
                }
            }
            break;
        }

        if (clone_bytes.empty()) {
            fprintf(stderr, "ERROR: could not extract item at bag %d slot %d\n", src_bag, src_slot);
            return 1;
        }

        fprintf(stderr, "Cloned item: %zu bytes from bag[%d].slot[%d]\n", clone_bytes.size(), src_bag, src_slot);
        fprintf(stderr, "  field offsets: key=+0x%X no=+0x%X count=+0x%X transfer=+0x%X\n",
            orig_key_offset, orig_no_offset, orig_count_offset, orig_transfer_offset);

        // Patch fields
        if (orig_key_offset && orig_key_offset + 4 <= clone_bytes.size()) {
            memcpy(clone_bytes.data() + orig_key_offset, &new_key, 4);
            fprintf(stderr, "  patched _itemKey -> %u\n", new_key);
        }
        if (orig_no_offset && orig_no_offset + 8 <= clone_bytes.size()) {
            uint64_t new_no = ((uint64_t)new_key << 32) | (uint64_t)(tree.blob.size() & 0xFFFFFFFF);
            memcpy(clone_bytes.data() + orig_no_offset, &new_no, 8);
            fprintf(stderr, "  patched _itemNo -> %llu\n", (unsigned long long)new_no);
        }
        if (new_count > 0 && orig_count_offset && orig_count_offset + 8 <= clone_bytes.size()) {
            memcpy(clone_bytes.data() + orig_count_offset, &new_count, 8);
            fprintf(stderr, "  patched _stackCount -> %llu\n", (unsigned long long)new_count);
        }
        if (orig_transfer_offset && orig_transfer_offset + 4 <= clone_bytes.size()) {
            uint32_t new_transfer = ((new_key & 0xFFFF) << 16) | 0x0101;
            memcpy(clone_bytes.data() + orig_transfer_offset, &new_transfer, 4);
            fprintf(stderr, "  patched _transferredItemKey -> 0x%08X\n", new_transfer);
        }

        // Insert via InsertNested
        char path[128];
        snprintf(path, sizeof(path), "_inventorylist[%d]._itemList", dst_bag);
        fprintf(stderr, "Inserting into: InventorySaveData.%s\n", path);

        size_t old_blob = tree.blob.size();
        auto result = InsertNested(tree, "InventorySaveData", path, clone_bytes);
        if (!result.ok) {
            fprintf(stderr, "FAILED: %s\n", result.error.c_str());
            return 1;
        }

        fprintf(stderr, "OK: growth=%d, new_index=%d, blob %zu -> %zu\n",
            result.growth, result.new_element_index, old_blob, tree.blob.size());

        // Verify
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "InventorySaveData") continue;
            for (auto& f : obj.fields) {
                if (f.name != "_inventorylist") continue;
                if (dst_bag < (int)f.list_elements.size()) {
                    for (auto& cf : f.list_elements[dst_bag].child_fields) {
                        if (cf.name == "_itemList")
                            fprintf(stderr, "Bag[%d] after: %zu items\n", dst_bag, cf.list_elements.size());
                    }
                }
            }
            break;
        }

        // Roundtrip + PO check
        {
            auto fresh = ParcSerializer::Serialize(tree.parsed, tree.blob);
            if (fresh.size() != tree.blob.size())
                fprintf(stderr, "ROUNDTRIP SIZE MISMATCH: %zu vs %zu\n", tree.blob.size(), fresh.size());
            else {
                int d = 0;
                for (size_t i = 0; i < fresh.size(); i++) if (fresh[i] != tree.blob[i]) d++;
                fprintf(stderr, "ROUNDTRIP: %s (%d diffs)\n", d == 0 ? "PERFECT MATCH" : "DIFFS", d);
            }
        }
        for (auto& obj : tree.parsed.objects) {
            if (obj.class_name != "FactionSpawnStageManagerSaveData") continue;
            int e = 0; for (auto& f : obj.fields) e += (int)f.list_elements.size();
            fprintf(stderr, "FactionSpawn: %d elements\n", e);
            break;
        }

        if (output) {
            WriteSave(tree, output);
            fprintf(stderr, "Written: %s\n", output);
        }
        return 0;
    }

    fprintf(stderr, "Unknown command: %s\n", cmd);
    return 1;
}
