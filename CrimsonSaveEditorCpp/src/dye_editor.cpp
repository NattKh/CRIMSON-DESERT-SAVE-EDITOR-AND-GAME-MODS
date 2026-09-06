#include "dye_editor.h"
#include "editor_common.h"
#include <cstdio>

using namespace EditorCommon;

namespace DyeEditor {

static std::vector<DyedItem> g_items;
static bool g_scanned = false;
static int g_selectedItem = -1;
static DyeSlot g_clipboard;
static bool g_hasClipboard = false;

static const char* kColorGroupNames[] = {
    "None", "Red", "Orange", "Yellow", "Green",
    "Blue", "Indigo", "Violet", "White", "Black", "Brown"
};
static const uint32_t kColorGroupKeys[] = {
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
};

static const char* kMaterialNames[] = {
    "None", "Leather", "Metal", "Cloth", "Wood",
    "Stone", "Bone", "Crystal", "Fur", "Scale", "Chain", "Silk"
};
static const uint16_t kMaterialKeys[] = {
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
};

static const DyePreset kPresets[] = {
    {"Pure Black",  0,   0,   0,   255, 9, 0, 0},
    {"Pure White",  255, 255, 255, 255, 8, 0, 0},
    {"Blood Red",   180, 20,  20,  255, 1, 0, 0},
    {"Royal Gold",  220, 180, 50,  255, 3, 2, 0},
    {"Deep Blue",   20,  40,  160, 255, 5, 0, 0},
    {"Forest Green",30,  120, 40,  255, 4, 0, 0},
    {"Shadow",      30,  30,  35,  255, 9, 2, 0},
    {"Ivory",       240, 235, 220, 255, 8, 3, 0},
    {"Crimson",     150, 10,  30,  255, 1, 2, 0},
    {"Midnight",    15,  15,  40,  255, 5, 2, 0},
};

// ── Helpers ──

// Using EC::RU32, EC::WU8, etc. from editor_common.h

// Uses EC::GetItemName from editor_common.h

// ── Scan ──

bool ScanDyeData(ParcEngine::SaveTree& tree) {
    g_items.clear();
    g_scanned = false;
    g_selectedItem = -1;

    // Recursive helper: search any field list for _itemKey and _itemDyeDataList
    std::function<void(const std::vector<SaveParserCpp::GenericFieldValue>&, uint32_t&,
                       const SaveParserCpp::GenericFieldValue*&)> findFields;
    findFields = [&](const std::vector<SaveParserCpp::GenericFieldValue>& fields,
                     uint32_t& item_key, const SaveParserCpp::GenericFieldValue*& dye_list) {
        for (auto& cf : fields) {
            if (!cf.present) continue;
            if (cf.name == "_itemKey") {
                item_key = RU32(tree.blob, cf.start_offset);
            }
            else if (cf.name == "_itemDyeDataList" &&
                     (!cf.list_elements.empty() || !cf.raw_value.empty())) {
                dye_list = &cf;
            }
            // Recurse into inline objects (e.g. _item wrapper)
            if ((cf.meta_kind == 4 || cf.meta_kind == 5) && !cf.child_fields.empty()) {
                findFields(cf.child_fields, item_key, dye_list);
            }
        }
    };

    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "EquipmentSaveData") continue;
        for (auto& fld : obj.fields) {
            if (fld.name != "_list" || fld.list_elements.empty()) continue;

            EC::Log("DyeScan: EquipmentSaveData._list has %zu elements, type='%s'",
                fld.list_elements.size(),
                fld.list_elements.empty() ? "" : fld.list_elements[0].child_type_name.c_str());

            // Debug: dump field names from first element recursively
            if (!fld.list_elements.empty()) {
                auto& dbg = fld.list_elements[0];
                EC::Log("DyeScan: elem[0] has %zu child_fields, meta_kind=%d", dbg.child_fields.size(), dbg.meta_kind);
                std::function<void(const std::vector<SaveParserCpp::GenericFieldValue>&, int)> dumpFields;
                dumpFields = [&](const std::vector<SaveParserCpp::GenericFieldValue>& fs, int depth) {
                    for (auto& f : fs) {
                        EC::Log("DyeScan:%*s '%s' present=%d mk=%d children=%zu list=%zu",
                            depth*2, "", f.name.c_str(), f.present, f.meta_kind,
                            f.child_fields.size(), f.list_elements.size());
                        if (depth < 3 && !f.child_fields.empty())
                            dumpFields(f.child_fields, depth + 1);
                    }
                };
                dumpFields(dbg.child_fields, 1);
            }

            for (int ei = 0; ei < (int)fld.list_elements.size(); ei++) {
                auto& equip_elem = fld.list_elements[ei];

                uint32_t item_key = 0;
                const SaveParserCpp::GenericFieldValue* dye_list = nullptr;

                // Search this element and all nested inline objects
                findFields(equip_elem.child_fields, item_key, dye_list);

                if (item_key && dye_list) {
                    DyedItem item;
                    item.item_key = item_key;
                    item.item_name = EC::GetItemName(item_key);
                    item.equip_index = ei;

                    for (auto& de : dye_list->list_elements) {
                        DyeSlot ds;
                        ds.elem_start = de.start_offset;
                        if (de.start_offset + 3 <= tree.blob.size())
                            ds.mask = tree.blob[de.start_offset + 2];

                        for (auto& dcf : de.child_fields) {
                            if (!dcf.present) continue;
                            if (dcf.name == "_dyeSlotNo") {
                                ds.slot = (int8_t)tree.blob[dcf.start_offset];
                                ds.off_slot = dcf.start_offset;
                            }
                            else if (dcf.name == "_dyeColorR") {
                                ds.r = tree.blob[dcf.start_offset];
                                ds.off_r = dcf.start_offset;
                            }
                            else if (dcf.name == "_dyeColorG") {
                                ds.g = tree.blob[dcf.start_offset];
                                ds.off_g = dcf.start_offset;
                            }
                            else if (dcf.name == "_dyeColorB") {
                                ds.b = tree.blob[dcf.start_offset];
                                ds.off_b = dcf.start_offset;
                            }
                            else if (dcf.name == "_dyeColorA") {
                                ds.a = tree.blob[dcf.start_offset];
                                ds.off_a = dcf.start_offset;
                            }
                            else if (dcf.name == "_grimeOpacity") {
                                ds.grime = (int8_t)tree.blob[dcf.start_offset];
                                ds.off_grime = dcf.start_offset;
                            }
                            else if (dcf.name == "_dyeColorGroupInfoKey") {
                                ds.color_group = RU32(tree.blob, dcf.start_offset);
                                ds.off_group = dcf.start_offset;
                            }
                            else if (dcf.name == "_texturePalleteKey") {
                                uint32_t sz = dcf.end_offset - dcf.start_offset;
                                if (sz == 2) {
                                    ds.material = RU16(tree.blob, dcf.start_offset);
                                } else {
                                    ds.material = (uint16_t)RU32(tree.blob, dcf.start_offset);
                                }
                                ds.off_material = dcf.start_offset;
                            }
                        }
                        item.slots.push_back(ds);
                    }
                    if (!item.slots.empty())
                        g_items.push_back(std::move(item));
                }
            }

            // Debug: if nothing found, log what fields exist on first element
            if (g_items.empty()) {
                auto& first = fld.list_elements[0];
                EC::Log("DyeScan: 0 dyed items found. First element type='%s', %zu child fields:",
                    first.child_type_name.c_str(), first.child_fields.size());
                for (auto& cf : first.child_fields) {
                    EC::Log("  field '%s' present=%d meta_kind=%d children=%zu list_elems=%zu",
                        cf.name.c_str(), cf.present, cf.meta_kind,
                        cf.child_fields.size(), cf.list_elements.size());
                }
            }
        }
        break;
    }

    EC::Log("DyeScan: found %zu dyed items", g_items.size());
    g_scanned = true;
    return !g_items.empty();
}

// All equipment items (including undyed) for the inject UI
struct EquipItem {
    uint32_t item_key = 0;
    std::string name;
    int equip_index = -1;
    bool has_dye = false;
};
static std::vector<EquipItem> g_allEquip;

static void ScanAllEquipment(ParcEngine::SaveTree& tree) {
    g_allEquip.clear();
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "EquipmentSaveData") continue;
        for (auto& fld : obj.fields) {
            if (fld.name != "_list" || fld.list_elements.empty()) continue;
            for (int ei = 0; ei < (int)fld.list_elements.size(); ei++) {
                auto& elem = fld.list_elements[ei];
                EquipItem eq;
                eq.equip_index = ei;
                // Search all levels for _itemKey and _itemDyeDataList
                std::function<void(const std::vector<SaveParserCpp::GenericFieldValue>&)> scan;
                scan = [&](const std::vector<SaveParserCpp::GenericFieldValue>& fields) {
                    for (auto& cf : fields) {
                        if (cf.name == "_itemKey" && cf.present)
                            eq.item_key = RU32(tree.blob, cf.start_offset);
                        if (cf.name == "_itemDyeDataList" && cf.present)
                            eq.has_dye = true;
                        if ((cf.meta_kind == 4 || cf.meta_kind == 5) && !cf.child_fields.empty())
                            scan(cf.child_fields);
                    }
                };
                scan(elem.child_fields);
                if (eq.item_key) {
                    eq.name = EC::GetItemName(eq.item_key);
                    g_allEquip.push_back(std::move(eq));
                }
            }
        }
        break;
    }
}

const std::vector<DyedItem>& GetDyedItems() { return g_items; }

// ── Dye Injection ──

bool InjectDye(ParcEngine::SaveTree& tree, int equip_index, int num_slots,
               uint8_t r, uint8_t g, uint8_t b, uint16_t material, uint32_t color_group) {
    // Find ItemDyeSaveData type index
    auto dye_ti_it = tree.name_to_type_idx.find("ItemDyeSaveData");
    if (dye_ti_it == tree.name_to_type_idx.end()) {
        EC::Log("DyeInject: FAILED — ItemDyeSaveData not in schema. Dye one item in-game first.");
        return false;
    }
    uint16_t dye_ti = (uint16_t)dye_ti_it->second;

    // Find the equipment item's _item field
    SaveParserCpp::GenericFieldValue* target_item = nullptr;
    uint32_t item_start = 0, item_end = 0;
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "EquipmentSaveData") continue;
        for (auto& fld : obj.fields) {
            if (fld.name != "_list" || equip_index >= (int)fld.list_elements.size()) continue;
            auto& equip = fld.list_elements[equip_index];
            for (auto& cf : equip.child_fields) {
                if (cf.name == "_item") {
                    target_item = &cf;
                    item_start = cf.start_offset;
                    item_end = cf.end_offset;
                    break;
                }
            }
        }
        break;
    }
    if (!target_item) {
        EC::Log("DyeInject: FAILED — equip[%d]._item not found", equip_index);
        return false;
    }

    // Check if already has dye
    uint32_t dye_insert_pos = 0;
    for (auto& icf : target_item->child_fields) {
        if (icf.name == "_itemDyeDataList" && icf.present) {
            EC::Log("DyeInject: item already has dye data — use color editor");
            return false;
        }
        if (icf.name == "_socketSaveDataList" && icf.present)
            dye_insert_pos = icf.end_offset - item_start;
        if (icf.name == "_dropResultSubSaveItemList" && icf.present && dye_insert_pos == 0)
            dye_insert_pos = icf.start_offset - item_start;
        if (icf.name == "_transferredItemKey" && icf.present && dye_insert_pos == 0)
            dye_insert_pos = icf.start_offset - item_start;
    }
    if (dye_insert_pos == 0) {
        EC::Log("DyeInject: FAILED — cannot determine insertion point");
        return false;
    }

    // Extract item bytes
    std::vector<uint8_t> item_bytes;
    if (!target_item->raw_value.empty())
        item_bytes = target_item->raw_value;
    else
        item_bytes.assign(tree.blob.begin() + item_start, tree.blob.begin() + item_end);

    // Set bit 14 in mask (byte[1] bit 6)
    uint16_t mbc = item_bytes[0] | (item_bytes[1] << 8);
    if (mbc < 2 || 2 + mbc > item_bytes.size()) {
        EC::Log("DyeInject: FAILED — bad mbc=%u", mbc);
        return false;
    }
    item_bytes[2 + 1] |= (1 << 6);

    // Build dye list
    std::vector<uint8_t> dye_list;
    auto push8 = [&](uint8_t v) { dye_list.push_back(v); };
    auto push16 = [&](uint16_t v) { dye_list.push_back(v&0xFF); dye_list.push_back((v>>8)&0xFF); };
    auto push32 = [&](uint32_t v) { for(int i=0;i<4;i++) dye_list.push_back((v>>(i*8))&0xFF); };

    // List header (18 bytes)
    push8(0);
    push8(num_slots & 0xFF); push8((num_slots>>8)&0xFF); push8((num_slots>>16)&0xFF);
    push32(0); push32(0); push32(0);
    push16(0);

    // Each dye slot element
    for (int s = 0; s < num_slots; s++) {
        push16(1); // mbc
        push8(0xFF); // mask = all fields
        push16(dye_ti);
        push8(0); // reserved
        for (int i = 0; i < 8; i++) push8(0xFF); // sentinel
        uint32_t po_off = (uint32_t)dye_list.size();
        push32(0); // PO placeholder
        uint32_t payload_start = (uint32_t)dye_list.size();
        memcpy(dye_list.data() + po_off, &payload_start, 4);
        push32(0); // reserved_u32
        push8((int8_t)s);      // _dyeSlotNo
        push8(r);              // _dyeColorR
        push8(g);              // _dyeColorG
        push8(b);              // _dyeColorB
        push8(255);            // _dyeColorA
        push8(0);              // _grimeOpacity
        push32(color_group);   // _dyeColorGroupInfoKey
        push16(material);      // _texturePalleteKey
        uint32_t ts = (uint32_t)dye_list.size() - payload_start;
        push32(ts);
    }

    // Insert dye list into item bytes
    item_bytes.insert(item_bytes.begin() + dye_insert_pos, dye_list.begin(), dye_list.end());

    // Fix trailing_size
    uint32_t old_ts = 0;
    memcpy(&old_ts, item_bytes.data() + item_bytes.size() - 4, 4);
    uint32_t new_ts = old_ts + (uint32_t)dye_list.size();
    memcpy(item_bytes.data() + item_bytes.size() - 4, &new_ts, 4);

    // Replace element
    auto result = ParcEngine::ReplaceElement(tree, "EquipmentSaveData",
        item_start, item_end, item_bytes);
    if (!result.ok) {
        EC::Log("DyeInject: ReplaceElement FAILED: %s", result.error.c_str());
        return false;
    }

    EC::Log("DyeInject: OK — %d dye slots added, growth=%d", num_slots, result.growth);
    return true;
}

// ── Apply preset to all slots of an item ──

static bool ApplyPreset(ParcEngine::SaveTree& tree, DyedItem& item, const DyePreset& preset) {
    bool changed = false;
    for (auto& ds : item.slots) {
        if (ds.off_r) { WU8(tree.blob, ds.off_r, preset.r); ds.r = preset.r; changed = true; }
        if (ds.off_g) { WU8(tree.blob, ds.off_g, preset.g); ds.g = preset.g; changed = true; }
        if (ds.off_b) { WU8(tree.blob, ds.off_b, preset.b); ds.b = preset.b; changed = true; }
        if (ds.off_a) { WU8(tree.blob, ds.off_a, preset.a); ds.a = preset.a; changed = true; }
        if (ds.off_group && preset.color_group) {
            WU32(tree.blob, ds.off_group, preset.color_group);
            ds.color_group = preset.color_group;
        }
        if (ds.off_material && preset.material) {
            WU16(tree.blob, ds.off_material, preset.material);
            ds.material = preset.material;
        }
        if (ds.off_grime) {
            WU8(tree.blob, ds.off_grime, (uint8_t)preset.grime);
            ds.grime = preset.grime;
        }
    }
    return changed;
}

// ── UI ──

static int g_injectSlots = 4;
static float g_injectColor[3] = {1.0f, 0.0f, 0.0f};

void RenderDyeTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_scanned) {
        ScanDyeData(tree);
        ScanAllEquipment(tree);
    }

    // Header
    ImGui::TextColored(ImVec4(0.4f, 1, 0.8f, 1), "Dye Editor");
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.6f, 1), "  %d dyed / %d equipment",
        (int)g_items.size(), (int)g_allEquip.size());
    ImGui::SameLine();
    if (ImGui::SmallButton("Rescan")) { ScanDyeData(tree); ScanAllEquipment(tree); }

    // Inject controls
    ImGui::SetNextItemWidth(80);
    ImGui::InputInt("Dye Slots", &g_injectSlots, 1, 1);
    if (g_injectSlots < 1) g_injectSlots = 1;
    if (g_injectSlots > 10) g_injectSlots = 10;
    ImGui::SameLine();
    ImGui::ColorEdit3("Color", g_injectColor, ImGuiColorEditFlags_NoInputs);

    ImGui::Separator();

    float panelW = ImGui::GetContentRegionAvail().x;
    float leftW = panelW * 0.30f;

    // LEFT: All equipment items — dyed have swatch, undyed have "Add Dye"
    ImGui::BeginChild("DyeItems", ImVec2(leftW, 0), true);

    // Dyed items first
    if (!g_items.empty()) {
        ImGui::TextColored(ImVec4(0.4f, 1, 0.6f, 1), "Dyed (%d)", (int)g_items.size());
        for (int i = 0; i < (int)g_items.size(); i++) {
            auto& item = g_items[i];
            ImGui::PushID(i);
            if (!item.slots.empty()) {
                auto& s0 = item.slots[0];
                ImVec4 col(s0.r / 255.f, s0.g / 255.f, s0.b / 255.f, 1.0f);
                ImGui::ColorButton("##swatch", col, ImGuiColorEditFlags_NoTooltip, ImVec2(14, 14));
                ImGui::SameLine();
            }
            char label[256];
            snprintf(label, sizeof(label), "%s (%d)##di", item.item_name.c_str(), (int)item.slots.size());
            if (ImGui::Selectable(label, g_selectedItem == i))
                g_selectedItem = i;
            ImGui::PopID();
        }
    }

    // Undyed equipment items
    bool anyUndyed = false;
    for (auto& eq : g_allEquip) if (!eq.has_dye) { anyUndyed = true; break; }
    if (anyUndyed) {
        ImGui::Separator();
        ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1), "Undyed Equipment");
        for (int i = 0; i < (int)g_allEquip.size(); i++) {
            auto& eq = g_allEquip[i];
            if (eq.has_dye) continue;
            ImGui::PushID(10000 + i);
            if (ImGui::SmallButton("Add Dye")) {
                uint8_t cr = (uint8_t)(g_injectColor[0] * 255.f);
                uint8_t cg = (uint8_t)(g_injectColor[1] * 255.f);
                uint8_t cb = (uint8_t)(g_injectColor[2] * 255.f);
                if (InjectDye(tree, eq.equip_index, g_injectSlots, cr, cg, cb, 0, 0)) {
                    dirty = true;
                    g_scanned = false; // force rescan
                }
            }
            ImGui::SameLine();
            ImGui::Text("%s", eq.name.c_str());
            ImGui::PopID();
        }
    }

    ImGui::EndChild();

    ImGui::SameLine();

    // RIGHT: Selected item dye editor
    ImGui::BeginChild("DyeSlots", ImVec2(0, 0), true);
    if (g_selectedItem >= 0 && g_selectedItem < (int)g_items.size()) {
        auto& item = g_items[g_selectedItem];
        ImGui::TextColored(ImVec4(1, 0.9f, 0.5f, 1), "%s", item.item_name.c_str());
        ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "Key: %u  |  %d dye slot(s)",
            item.item_key, (int)item.slots.size());

        // Presets bar
        ImGui::Separator();
        ImGui::Text("Presets:");
        ImGui::SameLine();
        for (int pi = 0; pi < (int)(sizeof(kPresets) / sizeof(kPresets[0])); pi++) {
            auto& p = kPresets[pi];
            ImVec4 pc(p.r / 255.f, p.g / 255.f, p.b / 255.f, 1.0f);
            ImGui::PushID(1000 + pi);
            if (ImGui::ColorButton(p.name.c_str(), pc, 0, ImVec2(20, 20))) {
                if (ApplyPreset(tree, item, p)) dirty = true;
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", p.name);
            ImGui::PopID();
            ImGui::SameLine();
        }
        ImGui::NewLine();

        // Copy/Paste all slots
        if (ImGui::SmallButton("Copy All")) {
            if (!item.slots.empty()) {
                g_clipboard = item.slots[0];
                g_hasClipboard = true;
            }
        }
        ImGui::SameLine();
        if (g_hasClipboard && ImGui::SmallButton("Paste to All Slots")) {
            for (auto& ds : item.slots) {
                if (ds.off_r) { WU8(tree.blob, ds.off_r, g_clipboard.r); ds.r = g_clipboard.r; }
                if (ds.off_g) { WU8(tree.blob, ds.off_g, g_clipboard.g); ds.g = g_clipboard.g; }
                if (ds.off_b) { WU8(tree.blob, ds.off_b, g_clipboard.b); ds.b = g_clipboard.b; }
                if (ds.off_a) { WU8(tree.blob, ds.off_a, g_clipboard.a); ds.a = g_clipboard.a; }
                if (ds.off_group) { WU32(tree.blob, ds.off_group, g_clipboard.color_group); ds.color_group = g_clipboard.color_group; }
                if (ds.off_material) { WU16(tree.blob, ds.off_material, g_clipboard.material); ds.material = g_clipboard.material; }
                if (ds.off_grime) { WU8(tree.blob, ds.off_grime, (uint8_t)g_clipboard.grime); ds.grime = g_clipboard.grime; }
            }
            dirty = true;
        }

        ImGui::Separator();

        // Per-slot editors
        for (int si = 0; si < (int)item.slots.size(); si++) {
            auto& ds = item.slots[si];
            ImGui::PushID(si);

            char slotLabel[64];
            snprintf(slotLabel, sizeof(slotLabel), "Slot %d", (int)ds.slot);
            if (ImGui::CollapsingHeader(slotLabel, ImGuiTreeNodeFlags_DefaultOpen)) {

                // Color picker
                float col[4] = {ds.r / 255.f, ds.g / 255.f, ds.b / 255.f, ds.a / 255.f};
                if (ImGui::ColorEdit4("Color", col,
                    ImGuiColorEditFlags_NoInputs | ImGuiColorEditFlags_AlphaBar |
                    ImGuiColorEditFlags_PickerHueWheel)) {
                    uint8_t nr = (uint8_t)(col[0] * 255.f);
                    uint8_t ng = (uint8_t)(col[1] * 255.f);
                    uint8_t nb = (uint8_t)(col[2] * 255.f);
                    uint8_t na = (uint8_t)(col[3] * 255.f);
                    if (ds.off_r && nr != ds.r) { WU8(tree.blob, ds.off_r, nr); ds.r = nr; dirty = true; }
                    if (ds.off_g && ng != ds.g) { WU8(tree.blob, ds.off_g, ng); ds.g = ng; dirty = true; }
                    if (ds.off_b && nb != ds.b) { WU8(tree.blob, ds.off_b, nb); ds.b = nb; dirty = true; }
                    if (ds.off_a && na != ds.a) { WU8(tree.blob, ds.off_a, na); ds.a = na; dirty = true; }
                }

                // RGB sliders (for precision)
                ImGui::SameLine();
                ImGui::BeginGroup();
                int ir = ds.r, ig = ds.g, ib = ds.b, ia = ds.a;
                ImGui::SetNextItemWidth(100);
                if (ImGui::SliderInt("R", &ir, 0, 255) && ds.off_r) {
                    ds.r = (uint8_t)ir; WU8(tree.blob, ds.off_r, ds.r); dirty = true;
                }
                ImGui::SetNextItemWidth(100);
                if (ImGui::SliderInt("G", &ig, 0, 255) && ds.off_g) {
                    ds.g = (uint8_t)ig; WU8(tree.blob, ds.off_g, ds.g); dirty = true;
                }
                ImGui::SetNextItemWidth(100);
                if (ImGui::SliderInt("B", &ib, 0, 255) && ds.off_b) {
                    ds.b = (uint8_t)ib; WU8(tree.blob, ds.off_b, ds.b); dirty = true;
                }
                ImGui::SetNextItemWidth(100);
                if (ImGui::SliderInt("A", &ia, 0, 255) && ds.off_a) {
                    ds.a = (uint8_t)ia; WU8(tree.blob, ds.off_a, ds.a); dirty = true;
                }
                ImGui::EndGroup();

                // Material dropdown
                if (ds.off_material) {
                    int matIdx = 0;
                    for (int mi = 0; mi < 12; mi++)
                        if (kMaterialKeys[mi] == ds.material) { matIdx = mi; break; }
                    ImGui::SetNextItemWidth(140);
                    if (ImGui::Combo("Material", &matIdx, kMaterialNames, 12)) {
                        ds.material = kMaterialKeys[matIdx];
                        WU16(tree.blob, ds.off_material, ds.material);
                        dirty = true;
                    }
                }

                // Color group dropdown
                if (ds.off_group) {
                    int grpIdx = 0;
                    for (int gi = 0; gi < 11; gi++)
                        if (kColorGroupKeys[gi] == ds.color_group) { grpIdx = gi; break; }
                    ImGui::SameLine();
                    ImGui::SetNextItemWidth(120);
                    if (ImGui::Combo("Group", &grpIdx, kColorGroupNames, 11)) {
                        ds.color_group = kColorGroupKeys[grpIdx];
                        WU32(tree.blob, ds.off_group, ds.color_group);
                        dirty = true;
                    }
                }

                // Grime slider
                if (ds.off_grime) {
                    int grime = (int)ds.grime;
                    ImGui::SetNextItemWidth(100);
                    if (ImGui::SliderInt("Grime", &grime, -128, 127)) {
                        ds.grime = (int8_t)grime;
                        WU8(tree.blob, ds.off_grime, (uint8_t)ds.grime);
                        dirty = true;
                    }
                }

                // Per-slot copy
                ImGui::SameLine(ImGui::GetContentRegionAvail().x - 60);
                if (ImGui::SmallButton("Copy")) {
                    g_clipboard = ds;
                    g_hasClipboard = true;
                }
                if (g_hasClipboard) {
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Paste")) {
                        if (ds.off_r) { WU8(tree.blob, ds.off_r, g_clipboard.r); ds.r = g_clipboard.r; }
                        if (ds.off_g) { WU8(tree.blob, ds.off_g, g_clipboard.g); ds.g = g_clipboard.g; }
                        if (ds.off_b) { WU8(tree.blob, ds.off_b, g_clipboard.b); ds.b = g_clipboard.b; }
                        if (ds.off_a) { WU8(tree.blob, ds.off_a, g_clipboard.a); ds.a = g_clipboard.a; }
                        if (ds.off_group) { WU32(tree.blob, ds.off_group, g_clipboard.color_group); ds.color_group = g_clipboard.color_group; }
                        if (ds.off_material) { WU16(tree.blob, ds.off_material, g_clipboard.material); ds.material = g_clipboard.material; }
                        if (ds.off_grime) { WU8(tree.blob, ds.off_grime, (uint8_t)g_clipboard.grime); ds.grime = g_clipboard.grime; }
                        dirty = true;
                    }
                }
            }
            ImGui::PopID();
        }
    } else {
        ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "Select an item to edit its dye.");
    }
    ImGui::EndChild();
}

} // namespace DyeEditor
