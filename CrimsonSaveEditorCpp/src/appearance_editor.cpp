#include "appearance_editor.h"
#include "editor_common.h"
#include <fstream>
#include <cstdio>

#ifdef _WIN32
#include <windows.h>
#include <commdlg.h>
#endif

using namespace EditorCommon;

namespace AppearanceEditor {

static AppearanceState g_as;

// Using EC::RU32 from editor_common.h

static void ParseCustomizationFields(
    const std::vector<SaveParserCpp::GenericFieldValue>& fields,
    const std::vector<uint8_t>& blob,
    AppearanceData& out)
{
    for (auto& f : fields) {
        if (!f.present) continue;
        uint32_t sz = f.end_offset - f.start_offset;
        if (f.name == "_meshData" && sz >= 5) {
            // Format: u8 prefix + u32 count + count bytes
            uint32_t count = RU32(blob, f.start_offset + 1);
            out.mesh_count_offset = f.start_offset + 1;
            out.mesh_offset = f.start_offset + 5;
            if (count <= 64 && f.start_offset + 5 + count <= blob.size()) {
                out.mesh_data.assign(blob.begin() + out.mesh_offset,
                                     blob.begin() + out.mesh_offset + count);
            }
        }
        else if (f.name == "_decorationData" && sz >= 5) {
            uint32_t count = RU32(blob, f.start_offset + 1);
            out.deco_count_offset = f.start_offset + 1;
            out.decoration_offset = f.start_offset + 5;
            if (count <= 1000 && f.start_offset + 5 + count <= blob.size()) {
                out.decoration_data.assign(blob.begin() + out.decoration_offset,
                                           blob.begin() + out.decoration_offset + count);
            }
        }
        else if (f.name == "_version") {
            out.version = RU32(blob, f.start_offset);
            out.version_offset = f.start_offset;
        }
    }
}

void ScanAppearance(ParcEngine::SaveTree& tree) {
    g_as = AppearanceState{};
    auto& blob = tree.blob;

    // Player customization
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name == "CustomizationSaveData") {
            g_as.player.is_player = true;
            g_as.player.owner_name = "Player (Kliff)";
            g_as.player.owner_key = 1;
            ParseCustomizationFields(obj.fields, blob, g_as.player);
            break;
        }
    }

    // Merc customizations
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "MercenaryClanSaveData") continue;
        for (auto& f : obj.fields) {
            if (f.name != "_mercenaryDataList" || !f.present) continue;
            for (auto& el : f.list_elements) {
                uint32_t char_key = 0;
                const SaveParserCpp::GenericFieldValue* cust = nullptr;
                for (auto& cf : el.child_fields) {
                    if (cf.name == "_characterKey" && cf.present)
                        char_key = RU32(blob, cf.start_offset);
                    if (cf.name == "_customizationSaveData" && cf.present && !cf.child_fields.empty())
                        cust = &cf;
                }
                if (char_key && cust) {
                    AppearanceData ad;
                    ad.owner_key = char_key;
                    std::string n = EC::GetItemName(char_key);
                    ad.owner_name = n.empty() ? ("Merc " + std::to_string(char_key)) : n;
                    ParseCustomizationFields(cust->child_fields, blob, ad);
                    if (!ad.mesh_data.empty())
                        g_as.mercenaries.push_back(std::move(ad));
                }
            }
            break;
        }
        break;
    }

    g_as.scanned = true;
}

// ── File I/O ──

static bool SaveAppearanceToFile(const AppearanceData& ad, const std::string& path) {
    std::ofstream f(path, std::ios::binary);
    if (!f.is_open()) return false;
    // Header: "CDAP" + version(u32) + mesh_size(u32) + deco_size(u32)
    f.write("CDAP", 4);
    uint32_t ver = 1;
    uint32_t msz = (uint32_t)ad.mesh_data.size();
    uint32_t dsz = (uint32_t)ad.decoration_data.size();
    f.write((char*)&ver, 4);
    f.write((char*)&msz, 4);
    f.write((char*)&dsz, 4);
    f.write((char*)ad.mesh_data.data(), msz);
    f.write((char*)ad.decoration_data.data(), dsz);
    return true;
}

static bool LoadAppearanceFromFile(const std::string& path, AppearanceData& out) {
    std::ifstream f(path, std::ios::binary);
    if (!f.is_open()) return false;
    char magic[4];
    f.read(magic, 4);
    if (memcmp(magic, "CDAP", 4) != 0) return false;
    uint32_t ver, msz, dsz;
    f.read((char*)&ver, 4);
    f.read((char*)&msz, 4);
    f.read((char*)&dsz, 4);
    if (msz > 256 || dsz > 4096) return false;
    out.mesh_data.resize(msz);
    out.decoration_data.resize(dsz);
    f.read((char*)out.mesh_data.data(), msz);
    f.read((char*)out.decoration_data.data(), dsz);
    return true;
}

static void WriteAppearanceToBlob(std::vector<uint8_t>& blob, const AppearanceData& target, const AppearanceData& source) {
    // Write mesh data
    uint32_t count = (uint32_t)source.mesh_data.size();
    if (target.mesh_offset && count == target.mesh_data.size()) {
        memcpy(&blob[target.mesh_offset], source.mesh_data.data(), count);
    }
    // Write decoration data
    count = (uint32_t)source.decoration_data.size();
    if (target.decoration_offset && count == target.decoration_data.size()) {
        memcpy(&blob[target.decoration_offset], source.decoration_data.data(), count);
    }
}

// ── UI ──

static int g_selectedChar = 0; // 0=player, 1+=merc index+1
static AppearanceData g_clipboard;
static bool g_hasClipboard = false;
static char g_statusMsg[256] = {};

static void RenderByteGrid(const char* label, std::vector<uint8_t>& data, uint32_t blob_offset,
                           std::vector<uint8_t>& blob, bool& dirty) {
    if (data.empty()) { ImGui::Text("%s: (empty)", label); return; }

    ImGui::Text("%s (%d bytes):", label, (int)data.size());

    // Show as editable hex grid
    int cols = 16;
    for (int row = 0; row < (int)data.size(); row += cols) {
        ImGui::Text("%03X:", row);
        ImGui::SameLine();
        for (int c = 0; c < cols && row + c < (int)data.size(); c++) {
            int idx = row + c;
            ImGui::SameLine();
            ImGui::PushID(idx);

            uint8_t val = data[idx];
            bool is_default = (val == 0xFF);
            bool is_zero = (val == 0x00);

            if (is_default)
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.4f, 0.4f, 0.4f, 1.0f));
            else if (is_zero)
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.6f, 0.6f, 0.3f, 1.0f));
            else
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.3f, 1.0f, 0.5f, 1.0f));

            char buf[4];
            snprintf(buf, sizeof(buf), "%02X", val);
            ImGui::SetNextItemWidth(22);
            if (ImGui::InputText("##b", buf, sizeof(buf),
                ImGuiInputTextFlags_CharsHexadecimal | ImGuiInputTextFlags_CharsUppercase)) {
                unsigned int newval = 0;
                if (sscanf(buf, "%X", &newval) == 1 && newval <= 255) {
                    data[idx] = (uint8_t)newval;
                    if (blob_offset && blob_offset + idx < blob.size()) {
                        blob[blob_offset + idx] = (uint8_t)newval;
                        dirty = true;
                    }
                }
            }
            ImGui::PopStyleColor();

            if (ImGui::IsItemHovered()) {
                ImGui::SetTooltip("Byte %d (0x%03X)\nValue: %d (0x%02X)\n%s",
                    idx, idx, val, val,
                    is_default ? "DEFAULT (0xFF = unchanged)" :
                    is_zero ? "ZERO" : "MODIFIED from default");
            }

            ImGui::PopID();
        }
    }
}

static AppearanceData* GetSelected() {
    if (g_selectedChar == 0) return &g_as.player;
    int idx = g_selectedChar - 1;
    if (idx >= 0 && idx < (int)g_as.mercenaries.size()) return &g_as.mercenaries[idx];
    return nullptr;
}

// ── Labeled customization controls (decoration slot map, cracked 2026-07-01) ──
// Each customization category = a byte index in _decorationData. 0xFF = default/unchanged.
// Map verified against real saves: tattoo type/color land exactly where the UFC ASI ParamBlocks predict,
// and body-scar byte 132 is already set on the default character. See project_decoration_slot_map memory.

// Write one decoration byte into both the in-memory copy and the live blob, bounds-checked.
// Returns false (no-op) if the offset is stale/out of range — prevents the historical write crash.
static bool SetDecoByte(AppearanceData& sel, std::vector<uint8_t>& blob, uint32_t idx, uint8_t value) {
    if (idx >= sel.decoration_data.size()) return false;
    if (!sel.decoration_offset) return false;
    uint32_t abs = sel.decoration_offset + idx;
    if (abs >= blob.size()) return false;            // stale offset guard
    sel.decoration_data[idx] = value;
    blob[abs] = value;
    return true;
}

// A single labeled category: a "type" dropdown (Default + N options) and an optional color/opacity stepper.
static void RenderDecoCategory(const char* label, AppearanceData& sel, std::vector<uint8_t>& blob,
                               uint32_t type_slot, int type_count, uint32_t color_slot,
                               const char* option_prefix, bool& dirty) {
    if (type_slot >= sel.decoration_data.size()) return;
    ImGui::PushID(label);
    ImGui::AlignTextToFramePadding();
    ImGui::Text("%s", label);
    ImGui::SameLine(140);

    uint8_t cur = sel.decoration_data[type_slot];
    // Build preview string for the current value.
    char preview[48];
    if (cur == 0xFF) snprintf(preview, sizeof(preview), "Default (off)");
    else             snprintf(preview, sizeof(preview), "%s %d", option_prefix, (int)cur + 1);

    ImGui::SetNextItemWidth(180);
    if (ImGui::BeginCombo("##type", preview)) {
        if (ImGui::Selectable("Default (off)", cur == 0xFF)) {
            if (SetDecoByte(sel, blob, type_slot, 0xFF)) dirty = true;
        }
        for (int i = 0; i < type_count; i++) {
            char item[48];
            snprintf(item, sizeof(item), "%s %d", option_prefix, i + 1);
            if (ImGui::Selectable(item, cur == (uint8_t)i)) {
                if (SetDecoByte(sel, blob, type_slot, (uint8_t)i)) dirty = true;
            }
        }
        ImGui::EndCombo();
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Writes _decorationData byte %u.\n0xFF = default/unchanged.", type_slot);

    // Optional color/palette stepper (only meaningful once a type is selected).
    if (color_slot && color_slot < sel.decoration_data.size()) {
        ImGui::SameLine();
        ImGui::Text("Color:");
        ImGui::SameLine();
        uint8_t col = sel.decoration_data[color_slot];
        int cval = (col == 0xFF) ? -1 : col;
        ImGui::SetNextItemWidth(90);
        if (ImGui::InputInt("##color", &cval)) {
            if (cval < -1) cval = -1;
            if (cval > 254) cval = 254;
            if (SetDecoByte(sel, blob, color_slot, cval < 0 ? 0xFF : (uint8_t)cval)) dirty = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Palette index for byte %u. -1 = default.", color_slot);
    }
    ImGui::PopID();
}

static void RenderCustomizationControls(AppearanceData& sel, std::vector<uint8_t>& blob, bool& dirty) {
    if (sel.decoration_data.size() < 141) {
        ImGui::TextColored(ImVec4(1, 0.5f, 0.3f, 1),
            "Decoration array too small (%d bytes) — labeled controls unavailable.",
            (int)sel.decoration_data.size());
        return;
    }
    ImGui::TextColored(ImVec4(0.5f, 1.0f, 0.9f, 1.0f), "Customization (writes decoration slots directly)");
    ImGui::TextColored(ImVec4(0.7f, 0.7f, 0.7f, 1.0f),
        "Bypasses the barber UI. Scars: adds them even though the game hides the scar tabs. "
        "Body Scar is proven; Face Scar is by symmetry — verify in-game.");
    ImGui::Separator();

    // Scars — the whole point of this feature. Head: 3 textures, Body(nude): 5 textures.
    RenderDecoCategory("Face Scar",  sel, blob, /*type*/13,  /*count*/3, /*color*/21,  "Head Scar", dirty);
    RenderDecoCategory("Body Scar",  sel, blob, /*type*/132, /*count*/5, /*color*/140, "Body Scar", dirty);
    ImGui::Spacing();
    // Tattoos — for completeness (these already work in-barber). Head: 33, Body(nude): 66.
    RenderDecoCategory("Face Tattoo", sel, blob, /*type*/63,  /*count*/33, /*color*/71,  "Face Tattoo", dirty);
    RenderDecoCategory("Body Tattoo", sel, blob, /*type*/114, /*count*/66, /*color*/122, "Body Tattoo", dirty);
    ImGui::Spacing();
    // Dirt/grime overlays share the same mechanism.
    RenderDecoCategory("Face Dirt",   sel, blob, /*type*/72,  /*count*/8,  /*color*/80,  "Face Dirt", dirty);
    RenderDecoCategory("Body Dirt",   sel, blob, /*type*/123, /*count*/8,  /*color*/131, "Body Dirt", dirty);
}

void RenderAppearanceTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_as.scanned) {
        ImGui::Text("Load a save first.");
        return;
    }
    auto& blob = tree.blob;

    ImGui::TextColored(ImVec4(0.4f, 1.0f, 0.8f, 1.0f), "Appearance Editor");
    ImGui::TextColored(ImVec4(1.0f, 0.85f, 0.3f, 1.0f),
        "EXPERIMENTAL: Edit character appearance presets. "
        "0xFF = default/unchanged. Non-FF bytes are customized values.");
    ImGui::TextColored(ImVec4(0.7f, 0.7f, 0.7f, 1.0f),
        "Use Export/Import to backup before experimenting. "
        "Reset to Defaults if a character mod broke your save.");
    ImGui::Separator();

    if (g_statusMsg[0]) {
        ImGui::TextColored(ImVec4(0.3f, 1.0f, 0.4f, 1.0f), "%s", g_statusMsg);
    }

    float panelW = ImGui::GetContentRegionAvail().x;
    float leftW = panelW * 0.25f;

    // LEFT: Character list
    ImGui::BeginChild("CharList", ImVec2(leftW, 0), true);
    ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1), "Characters");
    ImGui::Separator();

    // Player
    {
        int nondefault = 0;
        for (uint8_t b : g_as.player.mesh_data) if (b != 0xFF) nondefault++;
        char label[128];
        snprintf(label, sizeof(label), "Player (Kliff) [%d mod]", nondefault);
        if (ImGui::Selectable(label, g_selectedChar == 0))
            g_selectedChar = 0;
    }

    // Mercs
    for (int i = 0; i < (int)g_as.mercenaries.size(); i++) {
        auto& m = g_as.mercenaries[i];
        int nondefault = 0;
        for (uint8_t b : m.mesh_data) if (b != 0xFF) nondefault++;
        char label[128];
        snprintf(label, sizeof(label), "%s [%d mod]##m%d", m.owner_name.c_str(), nondefault, i);
        if (ImGui::Selectable(label, g_selectedChar == i + 1))
            g_selectedChar = i + 1;
    }
    ImGui::EndChild();

    ImGui::SameLine();

    // RIGHT: Editor
    ImGui::BeginChild("AppearEdit", ImVec2(0, 0), true);
    AppearanceData* sel = GetSelected();
    if (!sel) {
        ImGui::Text("Select a character.");
        ImGui::EndChild();
        return;
    }

    ImGui::TextColored(ImVec4(1, 0.9f, 0.5f, 1), "%s", sel->owner_name.c_str());
    ImGui::Text("Key: %u | Mesh: %dB | Decoration: %dB | Version: %u",
        sel->owner_key, (int)sel->mesh_data.size(), (int)sel->decoration_data.size(), sel->version);

    // Actions
    ImGui::Separator();
    if (ImGui::Button("Export (.cdap)")) {
#ifdef _WIN32
        OPENFILENAMEA ofn = {};
        char path[MAX_PATH] = {};
        snprintf(path, MAX_PATH, "%s_appearance.cdap", sel->owner_name.c_str());
        ofn.lStructSize = sizeof(ofn);
        ofn.lpstrFilter = "Appearance Files (*.cdap)\0*.cdap\0All Files\0*.*\0";
        ofn.lpstrFile = path;
        ofn.nMaxFile = MAX_PATH;
        ofn.Flags = OFN_OVERWRITEPROMPT;
        ofn.lpstrDefExt = "cdap";
        if (GetSaveFileNameA(&ofn)) {
            if (SaveAppearanceToFile(*sel, path))
                snprintf(g_statusMsg, sizeof(g_statusMsg), "Exported to %s", path);
            else
                snprintf(g_statusMsg, sizeof(g_statusMsg), "Export failed!");
        }
#endif
    }
    ImGui::SameLine();
    if (ImGui::Button("Import (.cdap)")) {
#ifdef _WIN32
        OPENFILENAMEA ofn = {};
        char path[MAX_PATH] = {};
        ofn.lStructSize = sizeof(ofn);
        ofn.lpstrFilter = "Appearance Files (*.cdap)\0*.cdap\0All Files\0*.*\0";
        ofn.lpstrFile = path;
        ofn.nMaxFile = MAX_PATH;
        ofn.Flags = OFN_FILEMUSTEXIST;
        if (GetOpenFileNameA(&ofn)) {
            AppearanceData imported;
            if (LoadAppearanceFromFile(path, imported)) {
                WriteAppearanceToBlob(blob, *sel, imported);
                sel->mesh_data = imported.mesh_data;
                sel->decoration_data = imported.decoration_data;
                dirty = true;
                snprintf(g_statusMsg, sizeof(g_statusMsg), "Imported from %s", path);
            } else {
                snprintf(g_statusMsg, sizeof(g_statusMsg), "Import failed — invalid .cdap file");
            }
        }
#endif
    }
    ImGui::SameLine();
    if (ImGui::Button("Copy")) {
        g_clipboard = *sel;
        g_hasClipboard = true;
        snprintf(g_statusMsg, sizeof(g_statusMsg), "Copied %s appearance", sel->owner_name.c_str());
    }
    if (g_hasClipboard) {
        ImGui::SameLine();
        if (ImGui::Button("Paste")) {
            WriteAppearanceToBlob(blob, *sel, g_clipboard);
            sel->mesh_data = g_clipboard.mesh_data;
            sel->decoration_data = g_clipboard.decoration_data;
            dirty = true;
            snprintf(g_statusMsg, sizeof(g_statusMsg), "Pasted appearance to %s", sel->owner_name.c_str());
        }
    }
    ImGui::SameLine();
    if (ImGui::Button("Reset to Defaults")) {
        std::fill(sel->mesh_data.begin(), sel->mesh_data.end(), 0xFF);
        std::fill(sel->decoration_data.begin(), sel->decoration_data.end(), 0xFF);
        if (sel->mesh_offset)
            memset(&blob[sel->mesh_offset], 0xFF, sel->mesh_data.size());
        if (sel->decoration_offset)
            memset(&blob[sel->decoration_offset], 0xFF, sel->decoration_data.size());
        dirty = true;
        snprintf(g_statusMsg, sizeof(g_statusMsg), "Reset %s to all defaults (0xFF)", sel->owner_name.c_str());
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Sets ALL bytes to 0xFF (game default).\nUse this to fix saves broken by character mods.");

    ImGui::Separator();

    // Labeled customization controls (scars/tattoos/dirt) — the friendly editor.
    RenderCustomizationControls(*sel, blob, dirty);

    ImGui::Separator();

    // Raw hex grids below for power users / unmapped slots.
    if (ImGui::CollapsingHeader("Raw bytes (advanced)")) {
        RenderByteGrid("Mesh Data (character preset)", sel->mesh_data, sel->mesh_offset, blob, dirty);
        ImGui::Separator();
        RenderByteGrid("Decoration Data (tattoos/accessories)", sel->decoration_data, sel->decoration_offset, blob, dirty);
    }

    ImGui::EndChild();
}

} // namespace AppearanceEditor
