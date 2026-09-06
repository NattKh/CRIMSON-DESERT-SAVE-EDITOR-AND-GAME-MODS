#include "transplant_editor.h"
#include "save_repair.h"
#include "editor_common.h"
#include <map>
#include <set>
#include <filesystem>
#include <cstdio>
#include <cstring>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <commdlg.h>
#endif

namespace fs = std::filesystem;
using namespace EditorCommon;

namespace TransplantEditor {

// ── Static state ──

static ParcEngine::SaveTree g_donorTree;
static bool g_donorLoaded = false;
static std::string g_donorPath;
static char g_donorPathBuf[512] = {};
static std::map<std::string, bool> g_selected;
static char g_status[512] = {};
static bool g_statusError = false;
static bool g_backedUp = false;

// ── Block info for the table ──

struct BlockRow {
    std::string name;
    uint32_t target_size = 0;
    int target_elems = 0;
    bool in_target = false;
    uint32_t donor_size = 0;
    int donor_elems = 0;
    bool in_donor = false;
};

static int CountElems(const SaveParserCpp::ObjectBlock& obj) {
    int n = 0;
    for (auto& f : obj.fields) n += (int)f.list_elements.size();
    return n;
}

static std::vector<BlockRow> BuildBlockTable(const ParcEngine::SaveTree& target,
                                              const ParcEngine::SaveTree& donor)
{
    std::map<std::string, BlockRow> rows;

    for (auto& obj : target.parsed.objects) {
        auto& r = rows[obj.class_name];
        r.name = obj.class_name;
        r.target_size = obj.data_size;
        r.target_elems += CountElems(obj);
        r.in_target = true;
    }
    for (auto& obj : donor.parsed.objects) {
        auto& r = rows[obj.class_name];
        r.name = obj.class_name;
        r.donor_size = obj.data_size;
        r.donor_elems += CountElems(obj);
        r.in_donor = true;
    }

    std::vector<BlockRow> out;
    out.reserve(rows.size());
    for (auto& [k, v] : rows) out.push_back(std::move(v));
    return out;
}

static void EnsureBackup(const std::string& savePath) {
    if (g_backedUp || savePath.empty()) return;
    try {
        std::string bak = savePath + ".pre_transplant.bak";
        if (fs::exists(savePath))
            fs::copy_file(savePath, bak, fs::copy_options::overwrite_existing);
        g_backedUp = true;
        EC::Log("Transplant backup: %s", bak.c_str());
    } catch (...) {}
}

static bool DoTransplant(ParcEngine::SaveTree& tree, const std::string& blockName,
                          bool& dirty, const std::string& savePath)
{
    EnsureBackup(savePath);
    bool ok = SaveRepair::TransplantBlock(tree, g_donorPath, blockName);
    if (ok) dirty = true;
    return ok;
}

// ── Render ──

void RenderTransplantTab(ParcEngine::SaveTree& tree, bool& dirty, const std::string& savePath) {
    ImGui::TextColored(ImVec4(1.0f, 0.65f, 0.3f, 1.0f), "Block Transplant");
    ImGui::TextColored(ImVec4(0.7f, 0.7f, 0.7f, 1.0f),
        "Transfer save data blocks from a donor save into the current save.");
    ImGui::TextColored(ImVec4(1.0f, 0.5f, 0.3f, 1.0f),
        "EXPERIMENTAL — Always backs up before modifying.");
    ImGui::Separator();

    if (g_status[0]) {
        ImVec4 col = g_statusError ? ImVec4(1, 0.3f, 0.3f, 1) : ImVec4(0.3f, 1.0f, 0.4f, 1);
        ImGui::TextColored(col, "%s", g_status);
        ImGui::Separator();
    }

    // ── Donor browse ──
    ImGui::Text("Donor Save:");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(400);
    ImGui::InputText("##donorpath", g_donorPathBuf, sizeof(g_donorPathBuf));
    ImGui::SameLine();
    if (ImGui::SmallButton("Browse##donor")) {
#ifdef _WIN32
        OPENFILENAMEA ofn = {};
        char path[MAX_PATH] = {};
        ofn.lStructSize = sizeof(ofn);
        ofn.lpstrFilter = "Save files (*.save)\0*.save\0All\0*.*\0";
        ofn.lpstrFile = path;
        ofn.nMaxFile = MAX_PATH;
        ofn.Flags = OFN_FILEMUSTEXIST;
        ofn.lpstrTitle = "Select Donor Save";
        if (GetOpenFileNameA(&ofn)) {
            strncpy(g_donorPathBuf, path, sizeof(g_donorPathBuf) - 1);
            try {
                g_donorTree = ParcEngine::LoadSave(path);
                g_donorLoaded = true;
                g_donorPath = path;
                g_selected.clear();
                g_backedUp = false;
                snprintf(g_status, sizeof(g_status), "Donor loaded: %s (%zu blocks, %zu types)",
                    fs::path(path).filename().string().c_str(),
                    g_donorTree.parsed.objects.size(),
                    g_donorTree.parsed.schema.types.size());
                g_statusError = false;
            } catch (const std::exception& e) {
                g_donorLoaded = false;
                snprintf(g_status, sizeof(g_status), "Failed to load donor: %s", e.what());
                g_statusError = true;
            }
        }
#endif
    }

    if (g_donorLoaded) {
        ImGui::SameLine();
        ImGui::TextColored(ImVec4(0.4f, 1.0f, 0.6f, 1.0f), "[Loaded]");
    }

    if (!g_donorLoaded) {
        ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.6f, 1.0f),
            "Browse for a donor .save file to begin.");
        return;
    }

    // Save summary
    ImGui::Text("Target: %zuB, %zu types, %zu blocks  |  Donor: %zuB, %zu types, %zu blocks",
        tree.blob.size(), tree.parsed.schema.types.size(), tree.parsed.objects.size(),
        g_donorTree.blob.size(), g_donorTree.parsed.schema.types.size(), g_donorTree.parsed.objects.size());

    ImGui::Separator();

    // ── Quick actions ──
    ImGui::TextColored(ImVec4(0.5f, 0.8f, 1.0f, 1.0f), "Quick Actions:");

    bool hasEquipBoth = false, hasInvBoth = false;
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name == "EquipmentSaveData") hasEquipBoth = true;
        if (obj.class_name == "InventorySaveData") hasInvBoth = true;
    }
    // Check donor too
    {
        bool de = false, di = false;
        for (auto& obj : g_donorTree.parsed.objects) {
            if (obj.class_name == "EquipmentSaveData") de = true;
            if (obj.class_name == "InventorySaveData") di = true;
        }
        hasEquipBoth = hasEquipBoth && de;
        hasInvBoth = hasInvBoth && di;
    }

    if (!(hasEquipBoth && hasInvBoth)) ImGui::BeginDisabled();
    if (ImGui::Button("Transfer Equipment & Bags", ImVec2(250, 0))) {
        int ok = 0, fail = 0;
        const char* blocks[] = {"EquipmentSaveData", "InventorySaveData"};
        for (auto* bn : blocks) {
            if (DoTransplant(tree, bn, dirty, savePath)) ok++; else fail++;
        }
        snprintf(g_status, sizeof(g_status), "Equipment+Bags: %d OK, %d failed", ok, fail);
        g_statusError = (fail > 0);
    }
    if (!(hasEquipBoth && hasInvBoth)) ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("Transplants EquipmentSaveData + InventorySaveData\nfrom donor in one click.");

    ImGui::SameLine();

    // Check mercenary group blocks in both saves
    bool hasMercGroup = true;
    {
        const char* mercBlocks[] = {"MercenaryClanSaveData", "FriendlySaveData"};
        for (auto* bn : mercBlocks) {
            bool inT = false, inD = false;
            for (auto& obj : tree.parsed.objects) if (obj.class_name == bn) { inT = true; break; }
            for (auto& obj : g_donorTree.parsed.objects) if (obj.class_name == bn) { inD = true; break; }
            if (!inT || !inD) { hasMercGroup = false; break; }
        }
    }

    if (!hasMercGroup) ImGui::BeginDisabled();
    if (ImGui::Button("Transfer Mercenaries", ImVec2(250, 0))) {
        int ok = 0, fail = 0;
        const char* blocks[] = {"MercenaryClanSaveData", "FriendlySaveData"};
        for (auto* bn : blocks) {
            if (DoTransplant(tree, bn, dirty, savePath)) ok++; else fail++;
        }
        snprintf(g_status, sizeof(g_status), "Mercenaries: %d OK, %d failed", ok, fail);
        g_statusError = (fail > 0);
    }
    if (!hasMercGroup) ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("Transplants MercenaryClanSaveData + FriendlySaveData\nfrom donor. Does NOT touch SubLevelSaveData\n(your character, location, and progression stay intact).");

    ImGui::Separator();

    // ── Block table ──
    auto blocks = BuildBlockTable(tree, g_donorTree);

    int selectedCount = 0;
    for (auto& b : blocks) if (g_selected[b.name]) selectedCount++;

    ImGui::Text("Blocks: %d total, %d selected", (int)blocks.size(), selectedCount);
    ImGui::SameLine();
    if (ImGui::SmallButton("All")) {
        for (auto& b : blocks)
            if (b.in_target && b.in_donor) g_selected[b.name] = true;
    }
    ImGui::SameLine();
    if (ImGui::SmallButton("None")) {
        for (auto& [k, v] : g_selected) v = false;
    }
    ImGui::SameLine();
    if (selectedCount == 0) ImGui::BeginDisabled();
    if (ImGui::Button("Transplant Selected")) {
        int ok = 0, fail = 0;
        for (auto& b : blocks) {
            if (!g_selected[b.name] || !b.in_target || !b.in_donor) continue;
            if (DoTransplant(tree, b.name, dirty, savePath)) ok++; else fail++;
        }
        snprintf(g_status, sizeof(g_status), "Transplanted: %d OK, %d failed", ok, fail);
        g_statusError = (fail > 0);
    }
    if (selectedCount == 0) ImGui::EndDisabled();

    float tableH = ImGui::GetContentRegionAvail().y - 10;
    if (tableH < 200) tableH = 200;

    if (ImGui::BeginTable("##transplant_blocks", 6,
        ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
        ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable |
        ImGuiTableFlags_SizingFixedFit,
        ImVec2(0, tableH)))
    {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Block", ImGuiTableColumnFlags_WidthStretch);
        ColFixed("Tgt Elems", 85);
        ColFixed("Tgt Size", 80);
        ColFixed("Dnr Elems", 85);
        ColFixed("Dnr Size", 80);
        ColFixed("##sel", 35);
        ImGui::TableHeadersRow();

        for (auto& b : blocks) {
            ImGui::TableNextRow();

            // Block name
            ImGui::TableSetColumnIndex(0);
            if (!b.in_target)
                ImGui::TextColored(ImVec4(1, 0.5f, 0.3f, 1), "%s (donor only)", b.name.c_str());
            else if (!b.in_donor)
                ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "%s", b.name.c_str());
            else {
                bool changed = (b.target_elems != b.donor_elems || b.target_size != b.donor_size);
                if (changed)
                    ImGui::TextColored(ImVec4(1.0f, 0.85f, 0.4f, 1), "%s", b.name.c_str());
                else
                    ImGui::Text("%s", b.name.c_str());
            }

            // Target
            ImGui::TableSetColumnIndex(1);
            if (b.in_target) ImGui::Text("%d", b.target_elems);
            else ImGui::TextDisabled("-");

            ImGui::TableSetColumnIndex(2);
            if (b.in_target) ImGui::Text("%u", b.target_size);
            else ImGui::TextDisabled("-");

            // Donor
            ImGui::TableSetColumnIndex(3);
            if (b.in_donor) ImGui::Text("%d", b.donor_elems);
            else ImGui::TextDisabled("-");

            ImGui::TableSetColumnIndex(4);
            if (b.in_donor) ImGui::Text("%u", b.donor_size);
            else ImGui::TextDisabled("-");

            // Checkbox
            ImGui::TableSetColumnIndex(5);
            if (b.in_target && b.in_donor) {
                bool sel = g_selected[b.name];
                char cbid[128];
                snprintf(cbid, sizeof(cbid), "##sel_%s", b.name.c_str());
                if (ImGui::Checkbox(cbid, &sel))
                    g_selected[b.name] = sel;
            }
        }

        ImGui::EndTable();
    }
}

} // namespace TransplantEditor
