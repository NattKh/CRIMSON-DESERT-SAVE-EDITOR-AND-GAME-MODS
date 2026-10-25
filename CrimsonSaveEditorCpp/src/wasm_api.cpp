/**
 * Crimson Desert Save Editor — browser edition
 * Copyright (c) 2026 NattKh. All rights reserved.
 * https://github.com/NattKh/CRIMSON-DESERT-SAVE-EDITOR
 * https://crimsondesertsaveedit.netlify.app
 *
 * wasm_api.cpp — WebAssembly bridge for the browser save editor (POC).
 *
 * Exposes the inventory-tab feature set through a C ABI consumed from JS:
 * load a save from bytes, list items as JSON, edit stack counts, swap item
 * keys, add new items (ItemFactory + InsertNested), export save.save and a
 * generated lobby.save.
 *
 * The engine is path-based, so the bridge stages byte buffers as files in
 * Emscripten's in-memory filesystem (MEMFS) and reuses ParcEngine unchanged.
 * Mirrors the desktop logic in inventory_editor.cpp — keep the two in sync:
 *  - item extraction = ExtractItems() walk
 *  - swap = itemKey + _transferredItemKey formula + enchant/sharpness reset
 *  - add  = ItemFactory::BuildItem + InsertNested("_inventorylist[cat]._itemList")
 */
#include "parc_engine.h"
#include "parc_xml.h"
#include "item_factory.h"
#include <cstdio>
#include <cstring>
#include <fstream>
#include <functional>
#include <string>
#include <vector>

#ifdef __EMSCRIPTEN__
#include <emscripten/emscripten.h>
#define WASM_EXPORT extern "C" EMSCRIPTEN_KEEPALIVE
#else
#define WASM_EXPORT extern "C"
#endif

namespace {

ParcEngine::SaveTree g_tree;
bool g_loaded = false;
std::string g_lastError;
std::string g_jsonOut;          // persistent buffer returned to JS
std::vector<uint8_t> g_fileOut; // persistent buffer for exported saves

constexpr const char* MEM_SAVE_IN = "/work_in.save";
constexpr const char* MEM_SAVE_OUT = "/work_out.save";

struct WasmItem {
    uint64_t itemNo = 0;
    uint32_t itemKey = 0;
    int64_t stackCount = 0;
    int slotNo = 0;
    int enchantLevel = 0;
    int endurance = 0;
    int sharpness = 0;
    int maxSockets = 0;
    int validSockets = 0;
    std::string source;
    uint32_t stackCountOffset = 0, stackCountSize = 0;
    uint32_t itemKeyOffset = 0, itemKeySize = 0;
    uint32_t itemNoOffset = 0, itemNoSize = 0;
    uint32_t slotNoOffset = 0, slotNoSize = 0;
    uint32_t enchantOffset = 0;
    uint32_t sharpnessOffset = 0;
    uint32_t xferKeyOffset = 0, xferKeySize = 0;
    // Clone support: the element's byte range + which list it lives in.
    uint32_t elemStart = 0, elemEnd = 0;
    std::string parentBlock;  // e.g. "InventorySaveData"
    std::string parentField;  // e.g. "_itemList"
};
std::vector<WasmItem> g_items;

uint64_t ReadFieldVal(const std::vector<uint8_t>& blob, uint32_t off, uint32_t end) {
    uint32_t sz = end - off;
    if (off == 0 || end == 0 || off >= blob.size() || sz > 8) return 0;
    uint64_t v = 0;
    memcpy(&v, blob.data() + off, sz);
    return v;
}

// Same walk as inventory_editor.cpp ExtractItems()
void ExtractItems() {
    g_items.clear();
    auto& blob = g_tree.blob;

    for (auto& obj : g_tree.parsed.objects) {
        bool isInventory = obj.class_name.find("InventorySaveData") != std::string::npos
            && obj.class_name.find("Contents") == std::string::npos;
        bool isEquipment = obj.class_name == "EquipmentSaveData";
        if (!isInventory && !isEquipment) continue;

        std::string blockSource = isEquipment ? "Equipment" : "Inventory";
        const std::string blockClass = obj.class_name;

        std::function<void(const std::vector<SaveParserCpp::GenericFieldValue>&)> walkFields;
        walkFields = [&](const std::vector<SaveParserCpp::GenericFieldValue>& fields) {
            for (auto& field : fields) {
                if (!field.present) continue;
                if ((field.meta_kind == 6 || field.meta_kind == 7) && !field.list_elements.empty()) {
                    for (auto& elem : field.list_elements) {
                        if (elem.child_type_name.find("ItemSaveData") != std::string::npos
                            && elem.child_type_name.find("CharacterConversion") == std::string::npos) {
                            WasmItem di;
                            di.elemStart = elem.start_offset;
                            di.elemEnd = elem.end_offset;
                            di.parentBlock = blockClass;
                            di.parentField = field.name;
                            for (auto& nf : elem.child_fields) {
                                if (!nf.present) continue;
                                if (nf.name == "_itemNo") {
                                    di.itemNo = ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.itemNoOffset = nf.start_offset;
                                    di.itemNoSize = nf.end_offset - nf.start_offset;
                                } else if (nf.name == "_itemKey") {
                                    di.itemKey = (uint32_t)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.itemKeyOffset = nf.start_offset;
                                    di.itemKeySize = nf.end_offset - nf.start_offset;
                                } else if (nf.name == "_stackCount") {
                                    di.stackCount = (int64_t)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.stackCountOffset = nf.start_offset;
                                    di.stackCountSize = nf.end_offset - nf.start_offset;
                                } else if (nf.name == "_slotNo") {
                                    di.slotNo = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.slotNoOffset = nf.start_offset;
                                    di.slotNoSize = nf.end_offset - nf.start_offset;
                                } else if (nf.name == "_enchantLevel") {
                                    di.enchantLevel = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.enchantOffset = nf.start_offset;
                                } else if (nf.name == "_endurance")
                                    di.endurance = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                else if (nf.name == "_sharpness") {
                                    di.sharpness = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                    di.sharpnessOffset = nf.start_offset;
                                } else if (nf.name == "_maxSocketCount")
                                    di.maxSockets = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                else if (nf.name == "_validSocketCount")
                                    di.validSockets = (int)ReadFieldVal(blob, nf.start_offset, nf.end_offset);
                                else if (nf.name == "_transferredItemKey") {
                                    di.xferKeyOffset = nf.start_offset;
                                    di.xferKeySize = nf.end_offset - nf.start_offset;
                                }
                            }
                            if (di.itemNo == 0 && di.itemKey == 0) continue;
                            di.source = blockSource;
                            g_items.push_back(std::move(di));
                        }
                    }
                    for (auto& elem : field.list_elements)
                        if (!elem.child_fields.empty()) walkFields(elem.child_fields);
                }
                if ((field.meta_kind == 4 || field.meta_kind == 5) && !field.child_fields.empty())
                    walkFields(field.child_fields);
            }
        };
        walkFields(obj.fields);
    }
}

std::string JsonEscape(const std::string& s) {
    std::string o;
    for (char c : s) {
        if (c == '"' || c == '\\') { o += '\\'; o += c; }
        else if ((unsigned char)c < 0x20) { char b[8]; snprintf(b, 8, "\\u%04x", c); o += b; }
        else o += c;
    }
    return o;
}

bool WriteMemFile(const char* path, const uint8_t* data, int size) {
    std::ofstream f(path, std::ios::binary);
    if (!f) return false;
    f.write((const char*)data, size);
    return f.good();
}

bool ReadMemFile(const char* path, std::vector<uint8_t>& out) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) return false;
    out.resize((size_t)f.tellg());
    f.seekg(0);
    f.read((char*)out.data(), (std::streamsize)out.size());
    return f.good();
}

} // namespace

// ── Exported API ──

// Baked-in attribution — survives compilation as a real string in the .wasm,
// unlike source comments. Displayed by the page and serves as a provenance mark.
WASM_EXPORT const char* cse_about() {
    return "Crimson Desert Save Editor (browser edition) "
           "(c) 2026 NattKh. All rights reserved. "
           "https://crimsondesertsaveedit.netlify.app";
}

WASM_EXPORT const char* cse_last_error() { return g_lastError.c_str(); }

// Load a save from bytes. Returns 1 on success.
WASM_EXPORT int cse_load_save(const uint8_t* data, int size) {
    g_loaded = false;
    g_lastError.clear();
    try {
        if (!WriteMemFile(MEM_SAVE_IN, data, size)) {
            g_lastError = "MEMFS write failed";
            return 0;
        }
        g_tree = ParcEngine::LoadSave(MEM_SAVE_IN);
        ExtractItems();
        g_loaded = true;
        return 1;
    } catch (const std::exception& e) {
        g_lastError = e.what();
        return 0;
    }
}

// Item list as JSON: {"items":[{idx,itemNo,itemKey,stack,slot,enchant,
// endurance,sharpness,maxSockets,validSockets,source,canStack,canSwap},...]}
WASM_EXPORT const char* cse_get_items() {
    g_jsonOut = "{\"items\":[";
    for (size_t i = 0; i < g_items.size(); i++) {
        auto& d = g_items[i];
        char buf[512];
        snprintf(buf, sizeof(buf),
            "%s{\"idx\":%zu,\"itemNo\":%llu,\"itemKey\":%u,\"stack\":%lld,"
            "\"slot\":%d,\"enchant\":%d,\"endurance\":%d,\"sharpness\":%d,"
            "\"maxSockets\":%d,\"validSockets\":%d,\"source\":\"%s\","
            "\"canStack\":%s,\"canSwap\":%s}",
            i ? "," : "", i, (unsigned long long)d.itemNo, d.itemKey,
            (long long)d.stackCount, d.slotNo, d.enchantLevel, d.endurance,
            d.sharpness, d.maxSockets, d.validSockets,
            JsonEscape(d.source).c_str(),
            (d.stackCountOffset && d.stackCountSize) ? "true" : "false",
            (d.itemKeyOffset && d.itemKeySize) ? "true" : "false");
        g_jsonOut += buf;
    }
    g_jsonOut += "]}";
    return g_jsonOut.c_str();
}

// Change a stack count in place (same as desktop DoSave path). Returns 1 ok.
WASM_EXPORT int cse_set_stack(int idx, double newCount) {
    g_lastError.clear();
    if (!g_loaded || idx < 0 || (size_t)idx >= g_items.size()) { g_lastError = "bad index"; return 0; }
    auto& d = g_items[(size_t)idx];
    if (!d.stackCountOffset || !d.stackCountSize) { g_lastError = "item has no stack field"; return 0; }
    if (newCount < 1) newCount = 1;
    if (d.stackCountSize <= 4) {
        uint32_t v = (uint32_t)newCount;
        memcpy(g_tree.blob.data() + d.stackCountOffset, &v, d.stackCountSize);
    } else {
        uint64_t v = (uint64_t)newCount;
        memcpy(g_tree.blob.data() + d.stackCountOffset, &v, d.stackCountSize);
    }
    d.stackCount = (int64_t)newCount;
    return 1;
}

// Swap an item to a different itemKey — same formula as desktop swap popup:
// write key, recompute _transferredItemKey, zero enchant + sharpness.
WASM_EXPORT int cse_swap_item(int idx, int newKey) {
    g_lastError.clear();
    if (!g_loaded || idx < 0 || (size_t)idx >= g_items.size()) { g_lastError = "bad index"; return 0; }
    auto& d = g_items[(size_t)idx];
    if (!d.itemKeyOffset || !d.itemKeySize) { g_lastError = "item has no key field"; return 0; }

    uint32_t key = (uint32_t)newKey;
    memcpy(g_tree.blob.data() + d.itemKeyOffset, &key, d.itemKeySize);
    if (d.xferKeyOffset) {
        uint32_t tik = ((key & 0xFFFF) << 16) | 0x0101;
        memcpy(g_tree.blob.data() + d.xferKeyOffset, &tik, 4);
    }
    uint16_t zero16 = 0;
    if (d.enchantOffset) {
        memcpy(g_tree.blob.data() + d.enchantOffset, &zero16, 2);
        d.enchantLevel = 0;
    }
    if (d.sharpnessOffset) {
        memcpy(g_tree.blob.data() + d.sharpnessOffset, &zero16, 2);
        d.sharpness = 0;
    }
    d.itemKey = key;
    return 1;
}

// Add a new item by CLONING an existing one — the proven duplicate+swap
// pattern (mirrors inventory_editor.cpp's "Duplicate as…" path). We copy a
// real, game-authored item's bytes, patch key/itemNo/slot/stack/xferKey, zero
// enchant+sharpness, and splice it into that donor's own list. This reuses a
// structure the game already accepts, unlike building an item from scratch.
//
// donorIdx = index into the item list to clone from (JS picks one whose
// structure matches the target: equipment donor for equipment, etc.).
// Returns 1 on success; re-extracts items so the new one appears.
WASM_EXPORT int cse_add_item(int donorIdx, int newKey, double stackCount) {
    g_lastError.clear();
    if (!g_loaded) { g_lastError = "no save loaded"; return 0; }
    if (donorIdx < 0 || (size_t)donorIdx >= g_items.size()) { g_lastError = "bad donor index"; return 0; }
    try {
        const WasmItem d = g_items[(size_t)donorIdx]; // copy: g_items is rebuilt below
        auto& blob = g_tree.blob;
        if (!d.elemStart || d.elemEnd <= d.elemStart || d.elemEnd > blob.size()) {
            g_lastError = "donor element range invalid";
            return 0;
        }
        if (d.parentBlock.empty() || d.parentField.empty()) {
            g_lastError = "donor parent list unknown";
            return 0;
        }

        std::vector<uint8_t> elem(blob.begin() + d.elemStart, blob.begin() + d.elemEnd);

        // Unique itemNo across all items; unique slot within the donor's source.
        uint64_t maxNo = 1000;
        for (auto& it : g_items) if (it.itemNo > maxNo) maxNo = it.itemNo;
        uint64_t newNo = maxNo + 1;
        int maxSlot = -1;
        for (auto& it : g_items)
            if (it.source == d.source && it.slotNo > maxSlot) maxSlot = it.slotNo;
        int newSlot = maxSlot + 1;

        auto patch = [&](uint32_t absOff, uint32_t sz, uint64_t val) {
            if (!absOff || sz == 0 || sz > 8) return;
            uint32_t rel = absOff - d.elemStart;
            if (rel + sz > elem.size()) return;
            memcpy(elem.data() + rel, &val, sz);
        };
        patch(d.itemKeyOffset, d.itemKeySize, (uint32_t)newKey);
        patch(d.itemNoOffset, d.itemNoSize, newNo);
        patch(d.slotNoOffset, d.slotNoSize, (uint64_t)(uint32_t)newSlot);
        patch(d.stackCountOffset, d.stackCountSize, (uint64_t)(stackCount < 1 ? 1 : stackCount));
        if (d.xferKeyOffset)
            patch(d.xferKeyOffset, d.xferKeySize ? d.xferKeySize : 4,
                  (((uint32_t)newKey & 0xFFFF) << 16) | 0x0101);
        patch(d.enchantOffset, 2, 0);
        patch(d.sharpnessOffset, 2, 0);

        auto result = ParcEngine::SpliceIntoList(g_tree, d.parentBlock, d.parentField,
                                                 elem, d.elemStart);
        if (!result.ok) { g_lastError = result.error; return 0; }

        // Persist + reload through MEMFS so offsets are fresh (desktop does
        // the same WriteSave + LoadSave cycle after insertion).
        ParcEngine::WriteSave(g_tree, MEM_SAVE_OUT);
        g_tree = ParcEngine::LoadSave(MEM_SAVE_OUT);
        ExtractItems();
        return 1;
    } catch (const std::exception& e) {
        g_lastError = e.what();
        return 0;
    }
}

// Export the current state as an encrypted save.save.
// Returns byte count (use cse_file_ptr to read), 0 on error.
WASM_EXPORT int cse_export_save() {
    g_lastError.clear();
    if (!g_loaded) { g_lastError = "no save loaded"; return 0; }
    try {
        ParcEngine::WriteSave(g_tree, MEM_SAVE_OUT);
        if (!ReadMemFile(MEM_SAVE_OUT, g_fileOut)) { g_lastError = "MEMFS read failed"; return 0; }
        return (int)g_fileOut.size();
    } catch (const std::exception& e) {
        g_lastError = e.what();
        return 0;
    }
}

// Generate a lobby.save with the given slot display name.
// Returns byte count (use cse_file_ptr), 0 on error.
WASM_EXPORT int cse_build_lobby(const char* displayName) {
    g_lastError.clear();
    try {
        std::vector<uint8_t> blob, header;
        auto err = ParcXml::BuildLobbySave(displayName ? displayName : "", blob, header);
        if (!err.empty()) { g_lastError = err; return 0; }
        ParcEngine::SaveTree lt;
        lt.blob = std::move(blob);
        lt.is_encrypted = true;
        lt.original_header = std::move(header);
        ParcEngine::WriteSave(lt, MEM_SAVE_OUT);
        if (!ReadMemFile(MEM_SAVE_OUT, g_fileOut)) { g_lastError = "MEMFS read failed"; return 0; }
        return (int)g_fileOut.size();
    } catch (const std::exception& e) {
        g_lastError = e.what();
        return 0;
    }
}

// Pointer to the last exported file's bytes (valid until next export call).
WASM_EXPORT const uint8_t* cse_file_ptr() { return g_fileOut.data(); }

// Heap helpers for passing byte buffers in from JS.
WASM_EXPORT uint8_t* cse_alloc(int size) { return (uint8_t*)malloc((size_t)size); }
WASM_EXPORT void cse_free(uint8_t* p) { free(p); }
