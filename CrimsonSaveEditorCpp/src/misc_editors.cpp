#include "misc_editors.h"
#include "imgui.h"
#include <cstring>
#include <algorithm>

extern std::string GetItemDisplayName(uint32_t key);

static uint32_t RU32(const std::vector<uint8_t>& b, uint32_t o) {
    if(o+4>b.size()) return 0; return b[o]|(b[o+1]<<8)|(b[o+2]<<16)|(b[o+3]<<24);
}
static uint16_t RU16(const std::vector<uint8_t>& b, uint32_t o) {
    if(o+2>b.size()) return 0; return b[o]|(b[o+1]<<8);
}
static uint64_t RU64(const std::vector<uint8_t>& b, uint32_t o) {
    if(o+8>b.size()) return 0; return (uint64_t)RU32(b,o)|((uint64_t)RU32(b,o+4)<<32);
}
static void WU8(std::vector<uint8_t>& b, uint32_t o, uint8_t v) { if(o<b.size()) b[o]=v; }
static void WU16(std::vector<uint8_t>& b, uint32_t o, uint16_t v) {
    if(o+2>b.size()) return; b[o]=v&0xFF; b[o+1]=(v>>8)&0xFF;
}
static void WU32(std::vector<uint8_t>& b, uint32_t o, uint32_t v) {
    if(o+4>b.size()) return; b[o]=v&0xFF; b[o+1]=(v>>8)&0xFF; b[o+2]=(v>>16)&0xFF; b[o+3]=(v>>24)&0xFF;
}
static void WU64(std::vector<uint8_t>& b, uint32_t o, uint64_t v) {
    WU32(b,o,(uint32_t)v); WU32(b,o+4,(uint32_t)(v>>32));
}

// ═══════════════════════════════════════════════════════════
// Store Editor
// ═══════════════════════════════════════════════════════════

namespace StoreEditor {

struct StoreEntry {
    uint16_t store_key = 0;
    uint64_t last_refresh = 0;
    uint32_t item_list_size = 0;
    uint32_t key_offset = 0;
    uint32_t refresh_offset = 0;
    int elem_index = -1;
};

static std::vector<StoreEntry> g_stores;
static bool g_scanned = false;

void ScanStores(ParcEngine::SaveTree& tree) {
    g_stores.clear();
    g_scanned = false;
    auto& blob = tree.blob;

    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "StoreSaveData") continue;
        for (auto& f : obj.fields) {
            if (f.name != "_storeDataList" || !f.present) continue;
            for (int i = 0; i < (int)f.list_elements.size(); i++) {
                auto& el = f.list_elements[i];
                StoreEntry se;
                se.elem_index = i;
                for (auto& cf : el.child_fields) {
                    if (!cf.present) continue;
                    if (cf.name == "_storeKey") { se.store_key = RU16(blob, cf.start_offset); se.key_offset = cf.start_offset; }
                    else if (cf.name == "_lastPriceRefreshFieldTime") { se.last_refresh = RU64(blob, cf.start_offset); se.refresh_offset = cf.start_offset; }
                    else if (cf.name == "_storeItemList") { se.item_list_size = cf.end_offset - cf.start_offset; }
                }
                if (se.store_key) g_stores.push_back(se);
            }
        }
        break;
    }
    g_scanned = true;
}

void RenderStoreTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_scanned) { ImGui::Text("Load a save first."); return; }
    auto& blob = tree.blob;

    ImGui::TextColored(ImVec4(0.4f,1,0.8f,1), "Store Editor");
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.6f,0.6f,0.6f,1), "  %d stores", (int)g_stores.size());

    if (ImGui::SmallButton("Reset All Refresh Timers")) {
        for (auto& se : g_stores) {
            if (se.refresh_offset) {
                WU64(blob, se.refresh_offset, 0);
                se.last_refresh = 0;
            }
        }
        dirty = true;
    }
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.5f,0.5f,0.5f,1), "(Forces all stores to restock)");

    ImGui::Separator();
    if (ImGui::BeginTable("Stores", 4,
        ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY|ImGuiTableFlags_Resizable,
        ImVec2(0, 0))) {
        ImGui::TableSetupScrollFreeze(0,1);
        ImGui::TableSetupColumn("Store Key", ImGuiTableColumnFlags_WidthFixed, 80);
        ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("Items Size", ImGuiTableColumnFlags_WidthFixed, 80);
        ImGui::TableSetupColumn("Reset Timer", ImGuiTableColumnFlags_WidthFixed, 90);
        ImGui::TableHeadersRow();

        for (int i = 0; i < (int)g_stores.size(); i++) {
            auto& se = g_stores[i];
            ImGui::TableNextRow();
            ImGui::PushID(i);
            ImGui::TableNextColumn(); ImGui::Text("%u", se.store_key);
            ImGui::TableNextColumn();
            std::string name = GetItemDisplayName(se.store_key);
            ImGui::Text("%s", name.empty() ? "Store" : name.c_str());
            ImGui::TableNextColumn(); ImGui::Text("%uB", se.item_list_size);
            ImGui::TableNextColumn();
            if (se.last_refresh != 0) {
                if (ImGui::SmallButton("Reset")) {
                    WU64(blob, se.refresh_offset, 0);
                    se.last_refresh = 0;
                    dirty = true;
                }
            } else {
                ImGui::TextColored(ImVec4(0.2f,0.9f,0.2f,1), "Fresh");
            }
            ImGui::PopID();
        }
        ImGui::EndTable();
    }
}

} // namespace StoreEditor

// ═══════════════════════════════════════════════════════════
// Waypoint Editor
// ═══════════════════════════════════════════════════════════

namespace WaypointEditor {

struct WaypointEntry {
    uint32_t key = 0;
    float pos_x = 0, pos_y = 0, pos_z = 0;
    uint32_t key_offset = 0;
};

static std::vector<WaypointEntry> g_waypoints;
static bool g_scanned = false;
static int g_waypointCount = 0;

void ScanWaypoints(ParcEngine::SaveTree& tree) {
    g_waypoints.clear();
    g_scanned = false;
    g_waypointCount = 0;
    auto& blob = tree.blob;

    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "DiscoveredLevelGimmickSceneObjectSaveData") continue;
        for (auto& f : obj.fields) {
            if (f.name != "_discoveredLevelGimmickSceneObjectSaveDataList" || !f.present) continue;
            g_waypointCount = (int)f.list_elements.size();
            for (auto& el : f.list_elements) {
                WaypointEntry we;
                for (auto& cf : el.child_fields) {
                    if (!cf.present) continue;
                    if (cf.name == "_levelGimmickSceneObjectInfoKey") {
                        we.key = RU32(blob, cf.start_offset);
                        we.key_offset = cf.start_offset;
                    }
                    else if (cf.name == "_fogPivotPosition" && cf.end_offset - cf.start_offset >= 12) {
                        memcpy(&we.pos_x, &blob[cf.start_offset], 4);
                        memcpy(&we.pos_y, &blob[cf.start_offset+4], 4);
                        memcpy(&we.pos_z, &blob[cf.start_offset+8], 4);
                    }
                }
                if (we.key) g_waypoints.push_back(we);
            }
        }
        break;
    }
    g_scanned = true;
}

void RenderWaypointTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_scanned) { ImGui::Text("Load a save first."); return; }

    ImGui::TextColored(ImVec4(0.4f,1,0.8f,1), "Discovered Waypoints");
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.6f,0.6f,0.6f,1), "  %d discovered", g_waypointCount);

    ImGui::Separator();

    static char wpFilter[64] = {};
    ImGui::SetNextItemWidth(200);
    ImGui::InputText("Filter Key", wpFilter, sizeof(wpFilter));

    if (ImGui::BeginTable("Waypoints", 4,
        ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY|ImGuiTableFlags_Resizable,
        ImVec2(0, 0))) {
        ImGui::TableSetupScrollFreeze(0,1);
        ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 100);
        ImGui::TableSetupColumn("X", ImGuiTableColumnFlags_WidthFixed, 80);
        ImGui::TableSetupColumn("Y", ImGuiTableColumnFlags_WidthFixed, 80);
        ImGui::TableSetupColumn("Z", ImGuiTableColumnFlags_WidthFixed, 80);
        ImGui::TableHeadersRow();

        std::string filter = wpFilter;
        for (auto& we : g_waypoints) {
            if (!filter.empty()) {
                std::string ks = std::to_string(we.key);
                if (ks.find(filter) == std::string::npos) continue;
            }
            ImGui::TableNextRow();
            ImGui::TableNextColumn(); ImGui::Text("%u", we.key);
            ImGui::TableNextColumn(); ImGui::Text("%.0f", we.pos_x);
            ImGui::TableNextColumn(); ImGui::Text("%.0f", we.pos_y);
            ImGui::TableNextColumn(); ImGui::Text("%.0f", we.pos_z);
        }
        ImGui::EndTable();
    }
}

} // namespace WaypointEditor

// ═══════════════════════════════════════════════════════════
// Misc Editor (Cooldowns, Housing, Variables, Guides, Royal Supply)
// ═══════════════════════════════════════════════════════════

namespace MiscEditor {

struct MiscState {
    // ContentsMiscSaveData
    uint16_t housing_region = 0;
    uint64_t timewarp_cooldown = 0;
    int pin_count = 0;
    int alert_count = 0;
    int advice_count = 0;
    uint32_t housing_offset = 0;
    uint32_t timewarp_offset = 0;

    // GamePlayVariableSaveData
    struct GameVar {
        uint32_t key = 0;
        uint8_t value = 0;
        uint32_t key_offset = 0;
        uint32_t value_offset = 0;
    };
    std::vector<GameVar> game_vars;

    // RoyalSupplySaveData
    struct RoyalSupply {
        uint16_t key = 0;
        uint32_t item_key = 0;
        uint64_t remain_count = 0;
        uint32_t count_offset = 0;
    };
    std::vector<RoyalSupply> royal_supplies;

    // PlayGuideSaveData
    uint32_t guide_list_offset = 0;
    uint32_t guide_list_size = 0;

    bool scanned = false;
};

static MiscState g_misc;

void ScanMisc(ParcEngine::SaveTree& tree) {
    g_misc = MiscState{};
    auto& blob = tree.blob;

    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name == "ContentsMiscSaveData") {
            for (auto& f : obj.fields) {
                if (!f.present) continue;
                if (f.name == "_activatedHousingRegionKey") {
                    g_misc.housing_region = RU16(blob, f.start_offset);
                    g_misc.housing_offset = f.start_offset;
                }
                else if (f.name == "_timeWrapCoolTime") {
                    g_misc.timewarp_cooldown = RU64(blob, f.start_offset);
                    g_misc.timewarp_offset = f.start_offset;
                }
                else if (f.name == "_pinMarkerDataList") g_misc.pin_count = (int)f.list_elements.size();
                else if (f.name == "_alertHistorySaveDataList") g_misc.alert_count = (int)f.list_elements.size();
                else if (f.name == "_executedGameAdviceInfoKeyList") g_misc.advice_count = (int)f.list_elements.size();
            }
        }
        else if (obj.class_name == "GamePlayVariableSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_gamePlayVariableElementSaveData" && f.present) {
                    for (auto& el : f.list_elements) {
                        MiscState::GameVar gv;
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_gamePlayVariableKey") { gv.key = RU32(blob, cf.start_offset); gv.key_offset = cf.start_offset; }
                            else if (cf.name == "_currentVariable") { gv.value = blob[cf.start_offset]; gv.value_offset = cf.start_offset; }
                        }
                        if (gv.key) g_misc.game_vars.push_back(gv);
                    }
                }
            }
        }
        else if (obj.class_name == "RoyalSupplySaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_royalSupplyElementSaveDataList" && f.present) {
                    for (auto& el : f.list_elements) {
                        MiscState::RoyalSupply rs;
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_royalSupplyKey") rs.key = RU16(blob, cf.start_offset);
                            else if (cf.name == "_supplyItemKey") rs.item_key = RU32(blob, cf.start_offset);
                            else if (cf.name == "_remainSupplyCount") { rs.remain_count = RU64(blob, cf.start_offset); rs.count_offset = cf.start_offset; }
                        }
                        g_misc.royal_supplies.push_back(rs);
                    }
                }
            }
        }
        else if (obj.class_name == "PlayGuideSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_playGuideKeyList" && f.present) {
                    g_misc.guide_list_offset = f.start_offset;
                    g_misc.guide_list_size = f.end_offset - f.start_offset;
                }
            }
        }
    }
    g_misc.scanned = true;
}

void RenderMiscTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_misc.scanned) { ImGui::Text("Load a save first."); return; }
    auto& blob = tree.blob;

    // Cooldowns
    if (ImGui::CollapsingHeader("Cooldowns & Timers", ImGuiTreeNodeFlags_DefaultOpen)) {
        ImGui::Text("Timewarp Cooldown: %llu", (unsigned long long)g_misc.timewarp_cooldown);
        ImGui::SameLine();
        if (g_misc.timewarp_cooldown > 0 && ImGui::SmallButton("Reset##tw")) {
            WU64(blob, g_misc.timewarp_offset, 0);
            g_misc.timewarp_cooldown = 0;
            dirty = true;
        }

        int hr = (int)g_misc.housing_region;
        ImGui::SetNextItemWidth(80);
        if (ImGui::InputInt("Housing Region", &hr, 0, 0) && g_misc.housing_offset) {
            g_misc.housing_region = (uint16_t)hr;
            WU16(blob, g_misc.housing_offset, g_misc.housing_region);
            dirty = true;
        }

        ImGui::Text("Map Pins: %d", g_misc.pin_count);
        ImGui::Text("Alert History: %d entries", g_misc.alert_count);
        ImGui::Text("Game Advice Executed: %d", g_misc.advice_count);
        ImGui::Text("Play Guide Data: %uB", g_misc.guide_list_size);
    }

    // Game Variables
    if (ImGui::CollapsingHeader("Game Variables", ImGuiTreeNodeFlags_DefaultOpen)) {
        ImGui::TextColored(ImVec4(0.6f,0.6f,0.6f,1), "%d variables", (int)g_misc.game_vars.size());
        if (ImGui::BeginTable("GameVars", 3,
            ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg, ImVec2(0, 0))) {
            ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 100);
            ImGui::TableSetupColumn("Value", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableSetupColumn("Edit", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableHeadersRow();

            for (int i = 0; i < (int)g_misc.game_vars.size(); i++) {
                auto& gv = g_misc.game_vars[i];
                ImGui::TableNextRow();
                ImGui::PushID(i + 8000);
                ImGui::TableNextColumn(); ImGui::Text("%u", gv.key);
                ImGui::TableNextColumn(); ImGui::Text("%d", (int)gv.value);
                ImGui::TableNextColumn();
                int v = (int)gv.value;
                ImGui::SetNextItemWidth(60);
                if (ImGui::InputInt("##gv", &v, 0, 0) && gv.value_offset) {
                    gv.value = (uint8_t)v;
                    WU8(blob, gv.value_offset, gv.value);
                    dirty = true;
                }
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }

    // Royal Supply
    if (ImGui::CollapsingHeader("Royal Supply Wagons")) {
        for (int i = 0; i < (int)g_misc.royal_supplies.size(); i++) {
            auto& rs = g_misc.royal_supplies[i];
            ImGui::PushID(i + 9000);
            std::string iname = GetItemDisplayName(rs.item_key);
            ImGui::Text("Supply %u: %s — Remaining: %llu",
                rs.key, iname.empty() ? "Item" : iname.c_str(),
                (unsigned long long)rs.remain_count);
            ImGui::SameLine();
            if (ImGui::SmallButton("Refill (999)") && rs.count_offset) {
                WU64(blob, rs.count_offset, 999);
                rs.remain_count = 999;
                dirty = true;
            }
            ImGui::PopID();
        }
    }
}

} // namespace MiscEditor
