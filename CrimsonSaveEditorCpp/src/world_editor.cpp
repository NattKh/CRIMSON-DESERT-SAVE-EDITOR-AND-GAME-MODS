#include "world_editor.h"
#include "editor_common.h"
#include <cstring>

using namespace EditorCommon;

namespace WorldEditor {

static WorldState g_ws;

// ── Unified scan ──

void ScanAll(ParcEngine::SaveTree& tree) {
    g_ws = WorldState{};
    auto& blob = tree.blob;

    for (auto& obj : tree.parsed.objects) {
        // FactionSaveData
        if (obj.class_name == "FactionSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_factionElementSaveDataList" && f.present) {
                    for (auto& el : f.list_elements) {
                        FactionEntry fe;
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_ownerFactionKey") { fe.key = RU32(blob,cf.start_offset); fe.key_offset = cf.start_offset; }
                            else if (cf.name == "_relationGroupKey") { fe.relation_group = RU16(blob,cf.start_offset); fe.relation_offset = cf.start_offset; }
                            else if (cf.name == "_leaderCharacterKey") { fe.leader_char = RU32(blob,cf.start_offset); fe.leader_offset = cf.start_offset; }
                            else if (cf.name == "_factionRelationDataList") fe.relation_count = (int)cf.list_elements.size();
                        }
                        if (fe.key) g_ws.factions.push_back(fe);
                    }
                }
                if (f.name == "_factionNodeElementSaveDataList" && f.present) {
                    for (auto& el : f.list_elements) {
                        FactionNode fn{};
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_ownerFactionKey") fn.owner_faction = RU32(blob,cf.start_offset);
                            else if (cf.name == "_factionState") { fn.faction_state = blob[cf.start_offset]; fn.state_offset = cf.start_offset; }
                            else if (cf.name == "_conquerorFactionKey") { fn.conqueror = RU32(blob,cf.start_offset); fn.conqueror_offset = cf.start_offset; }
                            else if (cf.name == "_isCapital") fn.is_capital = blob[cf.start_offset];
                            else if (cf.name == "_blockSubType") fn.block_sub_type = blob[cf.start_offset];
                            else if (cf.name == "_operationStateType") { fn.operation_state = blob[cf.start_offset]; fn.operation_offset = cf.start_offset; }
                            else if (cf.name == "_enableNode") { fn.enable_node = blob[cf.start_offset]; fn.enable_offset = cf.start_offset; }
                        }
                        g_ws.faction_nodes.push_back(fn);
                    }
                }
            }
        }
        // FriendlySaveData
        else if (obj.class_name == "FriendlySaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_friendlyDataList" && f.present) {
                    for (auto& el : f.list_elements) {
                        FriendEntry fe{};
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_characterKey") { fe.character_key = RU32(blob,cf.start_offset); fe.char_key_offset = cf.start_offset; }
                            else if (cf.name == "_levelData" && !cf.child_fields.empty()) {
                                for (auto& lcf : cf.child_fields) {
                                    if (!lcf.present) continue;
                                    if (lcf.name == "_level") { fe.level = RU32(blob,lcf.start_offset); fe.level_offset = lcf.start_offset; }
                                    else if (lcf.name == "_exp") { fe.exp = RU64(blob,lcf.start_offset); fe.exp_offset = lcf.start_offset; }
                                }
                            }
                        }
                        if (fe.character_key) g_ws.friendships.push_back(fe);
                    }
                }
            }
        }
        // FieldNPCSaveData
        else if (obj.class_name == "FieldNPCSaveData") {
            FieldNPC npc{};
            for (auto& f : obj.fields) {
                if (!f.present) continue;
                if (f.name == "_spawnFieldInfoKey") npc.spawn_field = RU32(blob,f.start_offset);
                else if (f.name == "_fieldNpcSaveDataKey") npc.npc_key = RU32(blob,f.start_offset);
                else if (f.name == "_characterKey") { npc.character_key = RU32(blob,f.start_offset); npc.char_key_offset = f.start_offset; }
                else if (f.name == "_touchID") { npc.touch_id = RU64(blob,f.start_offset); npc.touch_offset = f.start_offset; }
                else if (f.name == "_friendly" && !f.child_fields.empty()) {
                    for (auto& cf : f.child_fields) {
                        if (!cf.present) continue;
                        if (cf.name == "_level") { npc.friendly_level = RU32(blob,cf.start_offset); npc.friendly_level_offset = cf.start_offset; }
                        else if (cf.name == "_exp") { npc.friendly_exp = RU64(blob,cf.start_offset); npc.friendly_exp_offset = cf.start_offset; }
                    }
                }
            }
            if (npc.character_key) g_ws.field_npcs.push_back(npc);
        }
        // SubLevelSaveData
        else if (obj.class_name == "SubLevelSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_list" && f.present) {
                    for (auto& el : f.list_elements) {
                        SubLevelEntry se{};
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_key") se.key = RU32(blob,cf.start_offset);
                            else if (cf.name == "_maxAchievedLevel") { se.max_level = RU32(blob,cf.start_offset); se.max_level_offset = cf.start_offset; }
                            else if (cf.name == "_level") { se.level = RU32(blob,cf.start_offset); se.level_offset = cf.start_offset; }
                            else if (cf.name == "_experience") { se.experience = RU64(blob,cf.start_offset); se.exp_offset = cf.start_offset; }
                        }
                        if (se.key) g_ws.sublevels.push_back(se);
                    }
                }
            }
        }
        // MercenaryClanSaveData
        else if (obj.class_name == "MercenaryClanSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_mercenaryDataList" && f.present) {
                    for (auto& el : f.list_elements) {
                        MercEntry me{};
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_characterKey") me.character_key = RU32(blob,cf.start_offset);
                            else if (cf.name == "_mercenaryNo") me.merc_no = RU64(blob,cf.start_offset);
                            else if (cf.name == "_lastSummoned") { me.last_summoned = blob[cf.start_offset]; me.summoned_offset = cf.start_offset; }
                            else if (cf.name == "_isInitialize") me.is_init = blob[cf.start_offset];
                            else if (cf.name == "_currentHp") { me.current_hp = RU64(blob,cf.start_offset); me.hp_offset = cf.start_offset; }
                            else if (cf.name == "_currentMp") { me.current_mp = RU64(blob,cf.start_offset); me.mp_offset = cf.start_offset; }
                            else if (cf.name == "_equipItemList") me.equip_count = (int)cf.list_elements.size();
                            else if (cf.name == "_inventoryItemList") me.inv_count = (int)cf.list_elements.size();
                        }
                        if (me.character_key) g_ws.mercenaries.push_back(me);
                    }
                }
            }
        }
        // StoreSaveData
        else if (obj.class_name == "StoreSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_storeDataList" && f.present) {
                    for (auto& el : f.list_elements) {
                        StoreEntry se{};
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_storeKey") se.store_key = RU16(blob,cf.start_offset);
                            else if (cf.name == "_lastPriceRefreshFieldTime") { se.last_refresh = RU64(blob,cf.start_offset); se.refresh_offset = cf.start_offset; }
                            else if (cf.name == "_storeItemList" && !cf.list_elements.empty()) {
                                for (auto& si : cf.list_elements) {
                                    StoreItem item{};
                                    for (auto& scf : si.child_fields) {
                                        if (!scf.present) continue;
                                        if (scf.name == "_tradeCount") { item.trade_count = RU64(blob,scf.start_offset); item.trade_offset = scf.start_offset; }
                                    }
                                    se.items.push_back(item);
                                }
                            }
                        }
                        if (se.store_key) g_ws.stores.push_back(se);
                    }
                }
            }
        }
        // DiscoveredLevelGimmickSceneObjectSaveData
        else if (obj.class_name == "DiscoveredLevelGimmickSceneObjectSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_discoveredLevelGimmickSceneObjectSaveDataList" && f.present) {
                    for (int i = 0; i < (int)f.list_elements.size(); i++) {
                        auto& el = f.list_elements[i];
                        WaypointEntry we{};
                        we.elem_index = i;
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_levelGimmickSceneObjectInfoKey") { we.key = RU32(blob,cf.start_offset); we.key_offset = cf.start_offset; }
                            else if (cf.name == "_fogPivotPosition" && cf.end_offset - cf.start_offset >= 12) {
                                memcpy(&we.pos_x, &blob[cf.start_offset], 4);
                                memcpy(&we.pos_y, &blob[cf.start_offset+4], 4);
                                memcpy(&we.pos_z, &blob[cf.start_offset+8], 4);
                            }
                        }
                        if (we.key) g_ws.waypoints.push_back(we);
                    }
                }
            }
        }
        // ContentsMiscSaveData
        else if (obj.class_name == "ContentsMiscSaveData") {
            for (auto& f : obj.fields) {
                if (!f.present) continue;
                if (f.name == "_activatedHousingRegionKey") { g_ws.housing_region = RU16(blob,f.start_offset); g_ws.housing_offset = f.start_offset; }
                else if (f.name == "_timeWrapCoolTime") { g_ws.timewarp_cooldown = RU64(blob,f.start_offset); g_ws.timewarp_offset = f.start_offset; }
                else if (f.name == "_pinMarkerDataList") g_ws.pin_count = (int)f.list_elements.size();
                else if (f.name == "_alertHistorySaveDataList") g_ws.alert_count = (int)f.list_elements.size();
            }
        }
        // GamePlayVariableSaveData
        else if (obj.class_name == "GamePlayVariableSaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_gamePlayVariableElementSaveData" && f.present) {
                    for (auto& el : f.list_elements) {
                        WorldState::GameVar gv{};
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_gamePlayVariableKey") gv.key = RU32(blob,cf.start_offset);
                            else if (cf.name == "_currentVariable") { gv.value = blob[cf.start_offset]; gv.val_offset = cf.start_offset; }
                        }
                        if (gv.key) g_ws.game_vars.push_back(gv);
                    }
                }
            }
        }
        // RoyalSupplySaveData
        else if (obj.class_name == "RoyalSupplySaveData") {
            for (auto& f : obj.fields) {
                if (f.name == "_royalSupplyElementSaveDataList" && f.present) {
                    for (auto& el : f.list_elements) {
                        WorldState::RoyalSupply rs{};
                        for (auto& cf : el.child_fields) {
                            if (!cf.present) continue;
                            if (cf.name == "_royalSupplyKey") rs.key = RU16(blob,cf.start_offset);
                            else if (cf.name == "_supplyItemKey") rs.item_key = RU32(blob,cf.start_offset);
                            else if (cf.name == "_remainSupplyCount") { rs.remain = RU64(blob,cf.start_offset); rs.remain_offset = cf.start_offset; }
                        }
                        g_ws.royal_supplies.push_back(rs);
                    }
                }
            }
        }
        // InventoryItemContentsSaveData
        else if (obj.class_name == "InventoryItemContentsSaveData") {
            for (auto& f : obj.fields) {
                if (!f.present) continue;
                if (f.name == "_investmentPropensity") { g_ws.investment_propensity = blob[f.start_offset]; g_ws.investment_offset = f.start_offset; }
            }
        }
    }
    g_ws.scanned = true;
}

// ═══════════════════════════════════════════════════════════
// World Tab (Factions + Friendship + NPCs + SubLevel + Mercs)
// ═══════════════════════════════════════════════════════════

void RenderWorldTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_ws.scanned) { ImGui::Text("Load a save first."); return; }
    auto& blob = tree.blob;

    ImGui::TextColored(ImVec4(1.0f, 0.85f, 0.3f, 1.0f),
        "EXPERIMENTAL: Some world state values may be recalculated by the game at runtime.");
    ImGui::Separator();

    // Factions
    if (ImGui::CollapsingHeader("Faction Nodes (1158)", ImGuiTreeNodeFlags_DefaultOpen)) {
        if (ImGui::BeginTable("FN", 6, ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY|ImGuiTableFlags_Resizable, ImVec2(0,220))) {
            ImGui::TableSetupScrollFreeze(0,1);
            ImGui::TableSetupColumn("Owner",ImGuiTableColumnFlags_WidthFixed,80); ImGui::TableSetupColumn("State",ImGuiTableColumnFlags_WidthFixed,50);
            ImGui::TableSetupColumn("Conqueror",ImGuiTableColumnFlags_WidthFixed,80); ImGui::TableSetupColumn("Op",ImGuiTableColumnFlags_WidthFixed,40);
            ImGui::TableSetupColumn("Cap",ImGuiTableColumnFlags_WidthFixed,35); ImGui::TableSetupColumn("En",ImGuiTableColumnFlags_WidthFixed,35);
            ImGui::TableHeadersRow();
            for (int i=0;i<(int)g_ws.faction_nodes.size();i++) {
                auto& fn = g_ws.faction_nodes[i];
                ImGui::TableNextRow(); ImGui::PushID(i);
                ImGui::TableNextColumn(); ImGui::Text("%u",fn.owner_faction);
                ImGui::TableNextColumn();
                int st=fn.faction_state; ImGui::SetNextItemWidth(35);
                if (ImGui::InputInt("##s",&st,0,0)&&fn.state_offset) { fn.faction_state=(uint8_t)st; WU8(blob,fn.state_offset,fn.faction_state); dirty=true; }
                ImGui::TableNextColumn();
                int cq=(int)fn.conqueror; ImGui::SetNextItemWidth(70);
                if (ImGui::InputInt("##c",&cq,0,0)&&fn.conqueror_offset) { fn.conqueror=(uint32_t)cq; WU32(blob,fn.conqueror_offset,fn.conqueror); dirty=true; }
                ImGui::TableNextColumn();
                int op=fn.operation_state; ImGui::SetNextItemWidth(30);
                if (ImGui::InputInt("##o",&op,0,0)&&fn.operation_offset) { fn.operation_state=(uint8_t)op; WU8(blob,fn.operation_offset,fn.operation_state); dirty=true; }
                ImGui::TableNextColumn(); ImGui::Text("%s",fn.is_capital?"Y":"N");
                ImGui::TableNextColumn();
                bool en=fn.enable_node!=0;
                if (ImGui::Checkbox("##e",&en)&&fn.enable_offset) { fn.enable_node=en?1:0; WU8(blob,fn.enable_offset,fn.enable_node); dirty=true; }
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }

    // NPC Friendships
    if (ImGui::CollapsingHeader("NPC Friendships (105)", ImGuiTreeNodeFlags_DefaultOpen)) {
        ImGui::TextColored(ImVec4(0.7f,0.7f,0.7f,1), "Level is saved. Daily interaction limits reset each day by the game.");
        if (ImGui::SmallButton("Max All Friendships")) {
            for (auto& fe : g_ws.friendships) {
                if (fe.level_offset) { fe.level = 10; WU32(blob,fe.level_offset,10); dirty = true; }
                if (fe.exp_offset) { fe.exp = 99999; WU64(blob,fe.exp_offset,99999); }
            }
        }
        if (ImGui::BeginTable("FR",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,200))) {
            ImGui::TableSetupScrollFreeze(0,1);
            ImGui::TableSetupColumn("Character",0); ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableSetupColumn("Level", ImGuiTableColumnFlags_WidthFixed, 70); ImGui::TableSetupColumn("XP", ImGuiTableColumnFlags_WidthFixed, 90);
            ImGui::TableHeadersRow();
            for (int i=0;i<(int)g_ws.friendships.size();i++) {
                auto& fe = g_ws.friendships[i]; ImGui::TableNextRow(); ImGui::PushID(i+3000);
                ImGui::TableNextColumn(); std::string n=EC::GetItemName(fe.character_key); ImGui::Text("%s",n.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%u",fe.character_key);
                ImGui::TableNextColumn();
                int lv=(int)fe.level; ImGui::SetNextItemWidth(50);
                if (ImGui::InputInt("##fl",&lv,0,0) && fe.level_offset) {
                    if(lv<0)lv=0; if(lv>20)lv=20;
                    fe.level=(uint32_t)lv; WU32(blob,fe.level_offset,fe.level); dirty=true;
                }
                ImGui::TableNextColumn();
                ImGui::Text("%llu",(unsigned long long)fe.exp);
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }

    // Field NPCs
    if (ImGui::CollapsingHeader("Field NPCs (135)")) {
        if (ImGui::SmallButton("Max All NPC Friendship")) {
            for (auto& npc : g_ws.field_npcs) {
                if (npc.friendly_level_offset) { npc.friendly_level = 10; WU32(blob,npc.friendly_level_offset,10); dirty = true; }
                if (npc.friendly_exp_offset) { npc.friendly_exp = 99999; WU64(blob,npc.friendly_exp_offset,99999); }
            }
        }
        if (ImGui::BeginTable("FNPC",5,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,200))) {
            ImGui::TableSetupScrollFreeze(0,1);
            ImGui::TableSetupColumn("Character",0); ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableSetupColumn("Field", ImGuiTableColumnFlags_WidthFixed, 50); ImGui::TableSetupColumn("Friend Lv", ImGuiTableColumnFlags_WidthFixed, 70);
            ImGui::TableSetupColumn("Friend XP", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableHeadersRow();
            for (int i=0;i<(int)g_ws.field_npcs.size();i++) {
                auto& npc = g_ws.field_npcs[i]; ImGui::TableNextRow(); ImGui::PushID(i+4000);
                ImGui::TableNextColumn(); std::string n=EC::GetItemName(npc.character_key); ImGui::Text("%s",n.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%u",npc.character_key);
                ImGui::TableNextColumn(); ImGui::Text("%u",npc.spawn_field);
                ImGui::TableNextColumn();
                int lv=(int)npc.friendly_level; ImGui::SetNextItemWidth(50);
                if (ImGui::InputInt("##nl",&lv,0,0) && npc.friendly_level_offset) {
                    if(lv<0)lv=0; if(lv>20)lv=20;
                    npc.friendly_level=(uint32_t)lv; WU32(blob,npc.friendly_level_offset,npc.friendly_level); dirty=true;
                }
                ImGui::TableNextColumn(); ImGui::Text("%llu",(unsigned long long)npc.friendly_exp);
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }

    // Region Progress
    if (ImGui::CollapsingHeader("Region Progress (16)", ImGuiTreeNodeFlags_DefaultOpen)) {
        if (ImGui::SmallButton("Max All Regions")) {
            for (auto& se : g_ws.sublevels) {
                if (se.level_offset) { se.level=100; WU32(blob,se.level_offset,100); se.max_level=100; WU32(blob,se.max_level_offset,100); dirty=true; }
            }
        }
        if (ImGui::BeginTable("SL",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg)) {
            ImGui::TableSetupColumn("Region", ImGuiTableColumnFlags_WidthFixed, 80); ImGui::TableSetupColumn("Level", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableSetupColumn("Max", ImGuiTableColumnFlags_WidthFixed, 60); ImGui::TableSetupColumn("XP", ImGuiTableColumnFlags_WidthFixed, 100);
            ImGui::TableHeadersRow();
            for (int i=0;i<(int)g_ws.sublevels.size();i++) {
                auto& se=g_ws.sublevels[i]; ImGui::TableNextRow(); ImGui::PushID(i+5000);
                ImGui::TableNextColumn(); ImGui::Text("%u",se.key);
                ImGui::TableNextColumn(); int lv=(int)se.level; ImGui::SetNextItemWidth(60);
                if (ImGui::InputInt("##lv",&lv,1,10)&&se.level_offset) {
                    if(lv<0)lv=0; if(lv>200)lv=200; se.level=(uint32_t)lv; WU32(blob,se.level_offset,se.level);
                    if(se.level>se.max_level){se.max_level=se.level; WU32(blob,se.max_level_offset,se.max_level);} dirty=true;
                }
                ImGui::TableNextColumn(); ImGui::Text("%u",se.max_level);
                ImGui::TableNextColumn(); ImGui::Text("%llu",(unsigned long long)se.experience);
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }

    // Mercenaries
    if (ImGui::CollapsingHeader("Mercenaries (78)")) {
        ImGui::TextColored(ImVec4(0.7f,0.7f,0.7f,1), "HP/MP may be recalculated when mercenary is summoned.");
        if (ImGui::BeginTable("MC",6,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY,ImVec2(0,200))) {
            ImGui::TableSetupScrollFreeze(0,1);
            ImGui::TableSetupColumn("Character",0); ImGui::TableSetupColumn("HP", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableSetupColumn("MP", ImGuiTableColumnFlags_WidthFixed, 60); ImGui::TableSetupColumn("Equip", ImGuiTableColumnFlags_WidthFixed, 45);
            ImGui::TableSetupColumn("Inv", ImGuiTableColumnFlags_WidthFixed, 40); ImGui::TableSetupColumn("Heal", ImGuiTableColumnFlags_WidthFixed, 50);
            ImGui::TableHeadersRow();
            for (int i=0;i<(int)g_ws.mercenaries.size();i++) {
                auto& me=g_ws.mercenaries[i]; ImGui::TableNextRow(); ImGui::PushID(i+6000);
                ImGui::TableNextColumn(); std::string n=EC::GetItemName(me.character_key); ImGui::Text("%s",n.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%llu",(unsigned long long)me.current_hp);
                ImGui::TableNextColumn(); ImGui::Text("%llu",(unsigned long long)me.current_mp);
                ImGui::TableNextColumn(); ImGui::Text("%d",me.equip_count);
                ImGui::TableNextColumn(); ImGui::Text("%d",me.inv_count);
                ImGui::TableNextColumn();
                if (me.hp_offset && ImGui::SmallButton("Full")) {
                    WU64(blob,me.hp_offset,9999); me.current_hp=9999;
                    WU64(blob,me.mp_offset,9999); me.current_mp=9999;
                    dirty=true;
                }
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }
}

// ═══════════════════════════════════════════════════════════
// Stores Tab
// ═══════════════════════════════════════════════════════════

void RenderStoresTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_ws.scanned) { ImGui::Text("Load a save first."); return; }
    auto& blob = tree.blob;

    ImGui::TextColored(ImVec4(0.4f,1,0.8f,1),"Store Editor"); ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.6f,0.6f,0.6f,1),"  %d stores",(int)g_ws.stores.size());

    ImGui::TextColored(ImVec4(0.7f,0.7f,0.7f,1),
        "TradeCount = how many times you purchased. Reset to 0 = full stock. "
        "Limits are defined in game data (storeinfo.pabgb), not the save.");
    ImGui::Separator();

    if (ImGui::SmallButton("Reset All Timers")) {
        for (auto& se : g_ws.stores) { if(se.refresh_offset) { WU64(blob,se.refresh_offset,0); se.last_refresh=0; } }
        dirty=true;
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Sets _lastPriceRefreshFieldTime to 0.\nForces price recalculation on next store visit.");
    ImGui::SameLine();
    if (ImGui::SmallButton("Clear All Purchase History")) {
        for (auto& se : g_ws.stores) {
            for (auto& si : se.items) { if(si.trade_offset) { WU64(blob,si.trade_offset,0); si.trade_count=0; } }
        }
        dirty=true;
    }
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Resets _tradeCount to 0 for every store item.\nMakes all limited-stock items available to buy again.");

    ImGui::Separator();
    if (ImGui::BeginTable("ST",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY|ImGuiTableFlags_Resizable)) {
        ImGui::TableSetupScrollFreeze(0,1);
        ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 70); ImGui::TableSetupColumn("Name",0);
        ImGui::TableSetupColumn("Items", ImGuiTableColumnFlags_WidthFixed, 50); ImGui::TableSetupColumn("Actions", ImGuiTableColumnFlags_WidthFixed, 140);
        ImGui::TableHeadersRow();
        for (int i=0;i<(int)g_ws.stores.size();i++) {
            auto& se=g_ws.stores[i]; ImGui::TableNextRow(); ImGui::PushID(i+7000);
            ImGui::TableNextColumn(); ImGui::Text("%u",se.store_key);
            ImGui::TableNextColumn(); std::string n=EC::GetItemName(se.store_key); ImGui::Text("%s",n.c_str());
            ImGui::TableNextColumn(); ImGui::Text("%d",(int)se.items.size());
            ImGui::TableNextColumn();
            if (se.last_refresh && ImGui::SmallButton("Timer")) { WU64(blob,se.refresh_offset,0); se.last_refresh=0; dirty=true; }
            if (!se.items.empty()) {
                ImGui::SameLine();
                if (ImGui::SmallButton("Clr History")) {
                    for (auto& si : se.items) { if(si.trade_offset) { WU64(blob,si.trade_offset,0); si.trade_count=0; } }
                    dirty=true;
                }
            }
            ImGui::PopID();
        }
        ImGui::EndTable();
    }
}

// ═══════════════════════════════════════════════════════════
// Waypoints Tab
// ═══════════════════════════════════════════════════════════

void RenderWaypointsTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_ws.scanned) { ImGui::Text("Load a save first."); return; }

    ImGui::TextColored(ImVec4(0.4f,1,0.8f,1),"Discovered Waypoints"); ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.6f,0.6f,0.6f,1),"  %d discovered",(int)g_ws.waypoints.size());
    ImGui::Separator();

    static char wpFilter[64]={};
    ImGui::SetNextItemWidth(200); ImGui::InputText("Filter",wpFilter,sizeof(wpFilter));

    if (ImGui::BeginTable("WP",4,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg|ImGuiTableFlags_ScrollY|ImGuiTableFlags_Resizable)) {
        ImGui::TableSetupScrollFreeze(0,1);
        ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 100); ImGui::TableSetupColumn("X", ImGuiTableColumnFlags_WidthFixed, 80);
        ImGui::TableSetupColumn("Y", ImGuiTableColumnFlags_WidthFixed, 80); ImGui::TableSetupColumn("Z", ImGuiTableColumnFlags_WidthFixed, 80);
        ImGui::TableHeadersRow();
        std::string filter=wpFilter;
        for (auto& we : g_ws.waypoints) {
            if (!filter.empty() && std::to_string(we.key).find(filter)==std::string::npos) continue;
            ImGui::TableNextRow();
            ImGui::TableNextColumn(); ImGui::Text("%u",we.key);
            ImGui::TableNextColumn(); ImGui::Text("%.0f",we.pos_x);
            ImGui::TableNextColumn(); ImGui::Text("%.0f",we.pos_y);
            ImGui::TableNextColumn(); ImGui::Text("%.0f",we.pos_z);
        }
        ImGui::EndTable();
    }
}

// ═══════════════════════════════════════════════════════════
// Misc Tab
// ═══════════════════════════════════════════════════════════

void RenderMiscTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_ws.scanned) { ImGui::Text("Load a save first."); return; }
    auto& blob = tree.blob;

    ImGui::TextColored(ImVec4(1.0f, 0.85f, 0.3f, 1.0f),
        "EXPERIMENTAL: Cooldowns and variables may be recalculated or overridden at runtime.");
    ImGui::Separator();

    if (ImGui::CollapsingHeader("Cooldowns & Timers", ImGuiTreeNodeFlags_DefaultOpen)) {
        ImGui::Text("Timewarp: %llu",(unsigned long long)g_ws.timewarp_cooldown); ImGui::SameLine();
        if (g_ws.timewarp_cooldown && ImGui::SmallButton("Reset##tw")) { WU64(blob,g_ws.timewarp_offset,0); g_ws.timewarp_cooldown=0; dirty=true; }
        int hr=(int)g_ws.housing_region; ImGui::SetNextItemWidth(80);
        if (ImGui::InputInt("Housing Region",&hr,0,0)&&g_ws.housing_offset) { g_ws.housing_region=(uint16_t)hr; WU16(blob,g_ws.housing_offset,g_ws.housing_region); dirty=true; }
        int ip=(int)g_ws.investment_propensity; ImGui::SetNextItemWidth(80);
        if (ImGui::InputInt("Investment Propensity",&ip,0,0)&&g_ws.investment_offset) { g_ws.investment_propensity=(uint8_t)ip; WU8(blob,g_ws.investment_offset,g_ws.investment_propensity); dirty=true; }
        ImGui::Text("Map Pins: %d | Alerts: %d",g_ws.pin_count,g_ws.alert_count);
    }

    if (ImGui::CollapsingHeader("Game Variables", ImGuiTreeNodeFlags_DefaultOpen)) {
        if (ImGui::BeginTable("GV",2,ImGuiTableFlags_Borders|ImGuiTableFlags_RowBg)) {
            ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 100); ImGui::TableSetupColumn("Value", ImGuiTableColumnFlags_WidthFixed, 80);
            ImGui::TableHeadersRow();
            for (int i=0;i<(int)g_ws.game_vars.size();i++) {
                auto& gv=g_ws.game_vars[i]; ImGui::TableNextRow(); ImGui::PushID(i+8000);
                ImGui::TableNextColumn(); ImGui::Text("%u",gv.key);
                ImGui::TableNextColumn(); int v=(int)gv.value; ImGui::SetNextItemWidth(60);
                if (ImGui::InputInt("##v",&v,0,0)&&gv.val_offset) { gv.value=(uint8_t)v; WU8(blob,gv.val_offset,gv.value); dirty=true; }
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }

    if (ImGui::CollapsingHeader("Royal Supply")) {
        for (int i=0;i<(int)g_ws.royal_supplies.size();i++) {
            auto& rs=g_ws.royal_supplies[i]; ImGui::PushID(i+9000);
            std::string n=EC::GetItemName(rs.item_key);
            ImGui::Text("Supply %u: %s — %llu left",rs.key,n.empty()?"Item":n.c_str(),(unsigned long long)rs.remain);
            ImGui::SameLine();
            if (ImGui::SmallButton("Refill 999")&&rs.remain_offset) { WU64(blob,rs.remain_offset,999); rs.remain=999; dirty=true; }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("Sets _remainSupplyCount to 999.\nMay be overridden when the royal supply wagon restocks.");
            ImGui::PopID();
        }
    }
}

} // namespace WorldEditor
