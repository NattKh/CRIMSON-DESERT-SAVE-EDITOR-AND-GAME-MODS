#include "knowledge_editor.h"
#include "editor_common.h"
#include <fstream>
#include <algorithm>
#include <cstring>

namespace KnowledgeEditor {

static KnowledgeGameData g_data;
static KnowledgeSaveState g_state;

// ── Data Loading ──

bool LoadGameData(const std::string& keys_path, const std::string& community_path, const std::string& groups_path) {
    // Load all knowledge entries
    {
        std::ifstream f(keys_path);
        if (!f.is_open()) return false;
        json j;
        try { j = json::parse(f); } catch (...) { return false; }
        for (auto& e : j) {
            KnowledgeDef def;
            def.key = e["key"];
            def.name = e.value("name", "");
            def.display = e.value("display_name", def.name);
            g_data.entries[def.key] = std::move(def);
        }
    }

    // Load community masks
    {
        std::ifstream f(community_path);
        if (f.is_open()) {
            json j;
            try { j = json::parse(f); } catch (...) {}
            for (auto& e : j) {
                int key = e["key"];
                std::string mask = e.value("mask", "");
                g_data.masks[key] = mask;
                if (g_data.entries.count(key))
                    g_data.entries[key].mask_hex = mask;
            }
        }
    }

    // Load group hierarchy
    {
        std::ifstream f(groups_path);
        if (f.is_open()) {
            json j;
            try { j = json::parse(f); } catch (...) { goto skip_groups; }
            for (auto& g : j) {
                KnowledgeGroup grp;
                grp.key = g["key"];
                grp.name = g.value("string_key", "");
                grp.parent = g.value("parent_knowledge_group_info", 0);
                grp.show_ui = g.value("is_show_ui", 1) != 0;
                grp.meditation_learnable = g.value("is_meditation_learnable", 0) != 0;
                if (g.contains("child_knowledge_group_info_list"))
                    for (auto& ck : g["child_knowledge_group_info_list"])
                        grp.children.push_back(ck.get<int>());
                if (g.contains("knowledge_info_list"))
                    for (auto& ek : g["knowledge_info_list"])
                        grp.knowledge_entries.push_back(ek.get<int>());

                // Resolve display name from pre-built lookup
                grp.display = EditorCommon::LookupDisplayName("knowledge_groups", grp.key);
                if (grp.display.empty()) {
                    // Fallback: clean string_key
                    grp.display = grp.name;
                    auto pos = grp.display.find("KnowledgeGroup_");
                    if (pos != std::string::npos) grp.display.erase(pos, 15);
                    std::replace(grp.display.begin(), grp.display.end(), '_', ' ');
                }

                g_data.groups[grp.key] = std::move(grp);
            }
            // Build top-level list
            for (auto& [k, grp] : g_data.groups) {
                if (grp.parent == 0 && grp.show_ui)
                    g_data.top_groups.push_back(grp);
            }
            std::sort(g_data.top_groups.begin(), g_data.top_groups.end(),
                [](const KnowledgeGroup& a, const KnowledgeGroup& b) { return a.key < b.key; });
        }
    }
skip_groups:

    g_data.loaded = true;
    return true;
}

// ── Save State Scanning ──

void ScanSaveState(ParcEngine::SaveTree& tree) {
    g_state = KnowledgeSaveState{};

    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "KnowledgeSaveData") continue;

        for (auto& field : obj.fields) {
            if (field.name != "_list" || !field.present) continue;

            for (int i = 0; i < (int)field.list_elements.size(); i++) {
                auto& elem = field.list_elements[i];
                KnowledgeState ks;
                ks.element_index = i;

                for (auto& cf : elem.child_fields) {
                    if (!cf.present) continue;
                    uint32_t sz = cf.end_offset - cf.start_offset;
                    if (cf.name == "_key" && sz >= 4) {
                        ks.key = (int)(tree.blob[cf.start_offset] |
                                      (tree.blob[cf.start_offset+1]<<8) |
                                      (tree.blob[cf.start_offset+2]<<16) |
                                      (tree.blob[cf.start_offset+3]<<24));
                        ks.key_offset = cf.start_offset;
                    }
                    else if (cf.name == "_level" && sz >= 4) {
                        ks.level = (int)(tree.blob[cf.start_offset] |
                                        (tree.blob[cf.start_offset+1]<<8) |
                                        (tree.blob[cf.start_offset+2]<<16) |
                                        (tree.blob[cf.start_offset+3]<<24));
                        ks.level_offset = cf.start_offset;
                    }
                }
                if (ks.key) {
                    g_state.learned[ks.key] = ks;
                    g_state.learned_keys.insert(ks.key);
                }
            }
            break;
        }
        break;
    }
    g_state.scanned = true;
}

// ── Editing Operations ──

bool LearnKnowledge(ParcEngine::SaveTree& tree, int key, int level) {
    if (g_state.learned_keys.count(key)) return false;

    // Find KnowledgeSaveData._list and clone last element
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "KnowledgeSaveData") continue;
        for (auto& field : obj.fields) {
            if (field.name != "_list" || !field.present) continue;
            if (field.list_elements.empty()) return false;

            // Get template from last element
            auto& tmpl_elem = field.list_elements.back();
            uint32_t tmpl_start = tmpl_elem.start_offset;
            uint32_t tmpl_end = tmpl_elem.end_offset;
            if (tmpl_end <= tmpl_start) return false;

            std::vector<uint8_t> element_bytes(
                tree.blob.begin() + tmpl_start,
                tree.blob.begin() + tmpl_end);

            // Patch _key in the cloned bytes
            for (auto& cf : tmpl_elem.child_fields) {
                if (cf.name == "_key" && cf.present) {
                    uint32_t off = cf.start_offset - tmpl_start;
                    if (off + 4 <= element_bytes.size()) {
                        element_bytes[off]   = (uint8_t)(key & 0xFF);
                        element_bytes[off+1] = (uint8_t)((key >> 8) & 0xFF);
                        element_bytes[off+2] = (uint8_t)((key >> 16) & 0xFF);
                        element_bytes[off+3] = (uint8_t)((key >> 24) & 0xFF);
                    }
                }
                else if (cf.name == "_level" && cf.present) {
                    uint32_t off = cf.start_offset - tmpl_start;
                    if (off + 4 <= element_bytes.size()) {
                        element_bytes[off]   = (uint8_t)(level & 0xFF);
                        element_bytes[off+1] = (uint8_t)((level >> 8) & 0xFF);
                        element_bytes[off+2] = (uint8_t)((level >> 16) & 0xFF);
                        element_bytes[off+3] = (uint8_t)((level >> 24) & 0xFF);
                    }
                }
            }

            // Use SpliceIntoList for insertion
            auto result = ParcEngine::SpliceIntoList(tree,
                "KnowledgeSaveData", "_list", element_bytes, tmpl_start);

            if (result.ok) {
                // Update state
                KnowledgeState ks;
                ks.key = key;
                ks.level = level;
                g_state.learned[key] = ks;
                g_state.learned_keys.insert(key);
                return true;
            }
            return false;
        }
        break;
    }
    return false;
}

bool UnlearnKnowledge(ParcEngine::SaveTree& tree, int key) {
    auto it = g_state.learned.find(key);
    if (it == g_state.learned.end()) return false;

    int idx = it->second.element_index;
    if (idx < 0) return false;

    auto result = ParcEngine::RemoveFromList(tree, "KnowledgeSaveData", "_list", idx);
    if (result.ok) {
        g_state.learned.erase(key);
        g_state.learned_keys.erase(key);
        // Re-scan to fix element indices
        ScanSaveState(tree);
        return true;
    }
    return false;
}

int LearnAllInGroup(ParcEngine::SaveTree& tree, int group_key) {
    auto git = g_data.groups.find(group_key);
    if (git == g_data.groups.end()) return 0;

    // Collect ALL keys to learn (this group + children) first
    std::vector<int> to_learn;
    std::function<void(int)> collectKeys = [&](int gk) {
        auto it = g_data.groups.find(gk);
        if (it == g_data.groups.end()) return;
        for (int ek : it->second.knowledge_entries) {
            if (!g_state.learned_keys.count(ek))
                to_learn.push_back(ek);
        }
        for (int ck : it->second.children)
            collectKeys(ck);
    };
    collectKeys(group_key);

    if (to_learn.empty()) return 0;

    // Find template element once
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "KnowledgeSaveData") continue;
        for (auto& field : obj.fields) {
            if (field.name != "_list" || !field.present || field.list_elements.empty())
                continue;

            auto& tmpl_elem = field.list_elements.back();
            uint32_t tmpl_start = tmpl_elem.start_offset;
            uint32_t tmpl_end = tmpl_elem.end_offset;
            if (tmpl_end <= tmpl_start) return 0;

            std::vector<uint8_t> base_bytes(
                tree.blob.begin() + tmpl_start,
                tree.blob.begin() + tmpl_end);

            // Find _key and _level offsets within the template
            uint32_t key_off = UINT32_MAX, level_off = UINT32_MAX;
            for (auto& cf : tmpl_elem.child_fields) {
                if (cf.name == "_key" && cf.present)
                    key_off = cf.start_offset - tmpl_start;
                else if (cf.name == "_level" && cf.present)
                    level_off = cf.start_offset - tmpl_start;
            }

            // Build all element bytes, then concatenate and insert once
            std::vector<uint8_t> all_bytes;
            all_bytes.reserve(base_bytes.size() * to_learn.size());
            for (int ek : to_learn) {
                auto elem = base_bytes;
                if (key_off != UINT32_MAX && key_off + 4 <= elem.size()) {
                    elem[key_off]   = (uint8_t)(ek & 0xFF);
                    elem[key_off+1] = (uint8_t)((ek >> 8) & 0xFF);
                    elem[key_off+2] = (uint8_t)((ek >> 16) & 0xFF);
                    elem[key_off+3] = (uint8_t)((ek >> 24) & 0xFF);
                }
                if (level_off != UINT32_MAX && level_off + 4 <= elem.size()) {
                    int lv = 1;
                    elem[level_off]   = (uint8_t)(lv & 0xFF);
                    elem[level_off+1] = (uint8_t)((lv >> 8) & 0xFF);
                    elem[level_off+2] = (uint8_t)((lv >> 16) & 0xFF);
                    elem[level_off+3] = (uint8_t)((lv >> 24) & 0xFF);
                }
                all_bytes.insert(all_bytes.end(), elem.begin(), elem.end());
            }

            // Insert one entry at a time — SpliceIntoList increments count by 1
            // and fixes all POs/trailing sizes per insertion. The tree is stale
            // after each splice but SpliceIntoList re-searches by name each call.
            int count = 0;
            for (int ek : to_learn) {
                auto elem = base_bytes;
                if (key_off != UINT32_MAX && key_off + 4 <= elem.size()) {
                    elem[key_off]   = (uint8_t)(ek & 0xFF);
                    elem[key_off+1] = (uint8_t)((ek >> 8) & 0xFF);
                    elem[key_off+2] = (uint8_t)((ek >> 16) & 0xFF);
                    elem[key_off+3] = (uint8_t)((ek >> 24) & 0xFF);
                }
                if (level_off != UINT32_MAX && level_off + 4 <= elem.size()) {
                    int lv = 1;
                    elem[level_off]   = (uint8_t)(lv & 0xFF);
                    elem[level_off+1] = (uint8_t)((lv >> 8) & 0xFF);
                    elem[level_off+2] = (uint8_t)((lv >> 16) & 0xFF);
                    elem[level_off+3] = (uint8_t)((lv >> 24) & 0xFF);
                }
                auto result = ParcEngine::SpliceIntoList(tree,
                    "KnowledgeSaveData", "_list", elem, 0);
                if (result.ok) {
                    g_state.learned_keys.insert(ek);
                    count++;
                }
            }
            if (count > 0) ScanSaveState(tree);
            return count;
        }
        break;
    }
    return 0;
}

int LearnAllAbyss(ParcEngine::SaveTree& tree) {
    // Abyss gates are in group key=3 (KnowledgeGroup_AbyssGate)
    int count = 0;
    auto git = g_data.groups.find(3);
    if (git == g_data.groups.end()) {
        // Fallback: learn all entries with "Abyss" or "Node" in name
        for (auto& [k, def] : g_data.entries) {
            if ((def.name.find("Abyss") != std::string::npos ||
                 def.name.find("Node_") != std::string::npos) &&
                !g_state.learned_keys.count(k)) {
                if (LearnKnowledge(tree, k, 0)) count++;
            }
        }
    } else {
        // Learn all in abyss group with level=0 (reveals gate without full knowledge)
        for (int ek : git->second.knowledge_entries) {
            if (!g_state.learned_keys.count(ek)) {
                if (LearnKnowledge(tree, ek, 0)) count++;
            }
        }
        for (int ck : git->second.children) {
            auto cit = g_data.groups.find(ck);
            if (cit != g_data.groups.end()) {
                for (int ek : cit->second.knowledge_entries) {
                    if (!g_state.learned_keys.count(ek)) {
                        if (LearnKnowledge(tree, ek, 0)) count++;
                    }
                }
            }
        }
    }
    return count;
}

int LearnDragonRiding(ParcEngine::SaveTree& tree) {
    static const int keys[] = {
        // Riding/Dragon core — the essential keys for dragon summon
        40038, 1000174, 1000175, 1000187, 1000189, 1000697, 1000720,
        1000948, 1001892, 1003893, 1004138, 1004154, 1004176, 1004177,
        1004178, 2147483119, 2147483121, 2147483122, 2147483123,
        2147483124, 2147483125, 2147483126, 2147483127, 2147483128,
        2147483130, 2147483131, 2147483132, 2147483133, 2147483134,
        2147483135,
        // Skill/Dye UI
        2601, 2602, 2603, 2617, 2618,
        // Special features
        1000560, 1001083, 1003311,
    };

    // Filter to only keys not already learned
    std::vector<int> toLearn;
    for (int k : keys) {
        if (!g_state.learned_keys.count(k)) toLearn.push_back(k);
    }
    if (toLearn.empty()) return 0;

    // Find KnowledgeSaveData._list and get the template element
    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "KnowledgeSaveData") continue;
        for (auto& field : obj.fields) {
            if (field.name != "_list" || !field.present) continue;
            if (field.list_elements.empty()) return 0;

            auto& tmpl_elem = field.list_elements.back();
            uint32_t tmpl_start = tmpl_elem.start_offset;
            uint32_t tmpl_end = tmpl_elem.end_offset;
            if (tmpl_end <= tmpl_start) return 0;

            uint32_t elem_size = tmpl_end - tmpl_start;
            std::vector<uint8_t> tmpl_bytes(
                tree.blob.begin() + tmpl_start,
                tree.blob.begin() + tmpl_end);

            // Find _key and _level offsets within the template
            uint32_t key_off = UINT32_MAX, level_off = UINT32_MAX;
            for (auto& cf : tmpl_elem.child_fields) {
                if (cf.name == "_key" && cf.present)
                    key_off = cf.start_offset - tmpl_start;
                else if (cf.name == "_level" && cf.present)
                    level_off = cf.start_offset - tmpl_start;
            }
            if (key_off == UINT32_MAX) return 0;

            // Build concatenated blob of all new elements
            std::vector<uint8_t> batch;
            batch.reserve(elem_size * toLearn.size());
            for (int k : toLearn) {
                std::vector<uint8_t> elem = tmpl_bytes;
                // Patch key
                if (key_off + 4 <= elem.size()) {
                    elem[key_off]   = (uint8_t)(k & 0xFF);
                    elem[key_off+1] = (uint8_t)((k >> 8) & 0xFF);
                    elem[key_off+2] = (uint8_t)((k >> 16) & 0xFF);
                    elem[key_off+3] = (uint8_t)((k >> 24) & 0xFF);
                }
                // Patch level = 1
                if (level_off != UINT32_MAX && level_off + 4 <= elem.size()) {
                    elem[level_off] = 1; elem[level_off+1] = 0;
                    elem[level_off+2] = 0; elem[level_off+3] = 0;
                }
                batch.insert(batch.end(), elem.begin(), elem.end());
            }

            // Single splice: insert all elements at once after the last element
            auto result = ParcEngine::SpliceIntoList(tree,
                "KnowledgeSaveData", "_list", batch, tmpl_start,
                (int)toLearn.size());

            if (result.ok) {
                for (int k : toLearn) {
                    KnowledgeState ks;
                    ks.key = k;
                    ks.level = 1;
                    g_state.learned[k] = ks;
                    g_state.learned_keys.insert(k);
                }
                return (int)toLearn.size();
            }
            return 0;
        }
        break;
    }
    return 0;
}

int LearnAll(ParcEngine::SaveTree& tree) {
    int count = 0;
    for (auto& [k, def] : g_data.entries) {
        if (!g_state.learned_keys.count(k)) {
            if (LearnKnowledge(tree, k, 1)) count++;
        }
    }
    return count;
}

const KnowledgeGameData& GetGameData() { return g_data; }
const KnowledgeSaveState& GetSaveState() { return g_state; }

// ── UI ──

static int g_selectedGroup = -1;
static char g_knowledgeFilter[128] = {};
static int g_showFilter = 0; // 0=all, 1=learned, 2=not learned

static void RenderGroupTree(const KnowledgeGroup& grp, ParcEngine::SaveTree& tree, bool& dirty, int depth = 0) {
    int total = (int)grp.knowledge_entries.size();
    int learned = 0;
    for (int ek : grp.knowledge_entries)
        if (g_state.learned_keys.count(ek)) learned++;

    // Count children recursively (approximate)
    for (int ck : grp.children) {
        auto cit = g_data.groups.find(ck);
        if (cit != g_data.groups.end()) {
            total += (int)cit->second.knowledge_entries.size();
            for (int ek : cit->second.knowledge_entries)
                if (g_state.learned_keys.count(ek)) learned++;
        }
    }

    char label[256];
    if (total > 0)
        snprintf(label, sizeof(label), "%s (%d/%d)##g%d", grp.display.c_str(), learned, total, grp.key);
    else
        snprintf(label, sizeof(label), "%s##g%d", grp.display.c_str(), grp.key);

    ImGuiTreeNodeFlags flags = ImGuiTreeNodeFlags_OpenOnArrow;
    if (grp.children.empty() && grp.knowledge_entries.empty())
        flags |= ImGuiTreeNodeFlags_Leaf;
    if (g_selectedGroup == grp.key)
        flags |= ImGuiTreeNodeFlags_Selected;

    bool open = ImGui::TreeNodeEx(label, flags);
    if (ImGui::IsItemClicked())
        g_selectedGroup = grp.key;

    // Right-click context menu
    if (ImGui::BeginPopupContextItem()) {
        char menuLabel[256];
        snprintf(menuLabel, sizeof(menuLabel), "Learn All in \"%s\"", grp.display.c_str());
        if (ImGui::MenuItem(menuLabel)) {
            int n = LearnAllInGroup(tree, grp.key);
            if (n > 0) dirty = true;
        }
        ImGui::EndPopup();
    }

    if (open) {
        for (int ck : grp.children) {
            auto cit = g_data.groups.find(ck);
            if (cit != g_data.groups.end() && cit->second.show_ui)
                RenderGroupTree(cit->second, tree, dirty, depth + 1);
        }
        ImGui::TreePop();
    }
}

void RenderKnowledgeTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_data.loaded) {
        ImGui::TextColored(ImVec4(1, 0.4f, 0.3f, 1), "Knowledge data not loaded.");
        return;
    }
    if (!g_state.scanned) {
        ImGui::TextColored(ImVec4(1, 0.8f, 0.3f, 1), "Load a save to view knowledge.");
        return;
    }

    // Top bar
    ImGui::TextColored(ImVec4(0.4f, 1, 0.6f, 1), "Knowledge Editor");
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.6f, 1), "  Learned: %d / %d",
        (int)g_state.learned_keys.size(), (int)g_data.entries.size());
    ImGui::SameLine();
    ImGui::TextDisabled("(Right-click a group to learn all in it)");

    ImGui::Separator();

    // Filter
    ImGui::SetNextItemWidth(200);
    ImGui::InputText("Search", g_knowledgeFilter, sizeof(g_knowledgeFilter));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(120);
    const char* filterOpts[] = {"All", "Learned", "Not Learned"};
    ImGui::Combo("Show##kf", &g_showFilter, filterOpts, 3);

    // Quick actions
    ImGui::SameLine(0, 20);
    if (ImGui::SmallButton("Learn Dragon Riding")) {
        int n = LearnDragonRiding(tree);
        if (n > 0) dirty = true;
        EC::Log("Dragon riding: learned %d knowledge entries", n);
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Learn ~120 knowledge entries required\nfor dragon mount summoning.\nUse after transplanting dragon from another save.");

    // Two-panel layout
    float panelW = ImGui::GetContentRegionAvail().x;
    float leftW = panelW * 0.35f;

    // LEFT: Group tree
    ImGui::BeginChild("KnowledgeGroups", ImVec2(leftW, 0), true);
    for (auto& grp : g_data.top_groups)
        RenderGroupTree(grp, tree, dirty);
    ImGui::EndChild();

    ImGui::SameLine();

    // RIGHT: Entries in selected group
    ImGui::BeginChild("KnowledgeEntries", ImVec2(0, 0), true);

    if (g_selectedGroup > 0) {
        auto git = g_data.groups.find(g_selectedGroup);
        if (git != g_data.groups.end()) {
            ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1), "%s", git->second.display.c_str());

            if (ImGui::SmallButton("Learn All in Group")) {
                int n = LearnAllInGroup(tree, g_selectedGroup);
                if (n > 0) dirty = true;
            }
            ImGui::Separator();

            // Collect entries (from this group + its children)
            std::vector<int> entries_to_show;
            entries_to_show.insert(entries_to_show.end(),
                git->second.knowledge_entries.begin(),
                git->second.knowledge_entries.end());
            for (int ck : git->second.children) {
                auto cit = g_data.groups.find(ck);
                if (cit != g_data.groups.end()) {
                    entries_to_show.insert(entries_to_show.end(),
                        cit->second.knowledge_entries.begin(),
                        cit->second.knowledge_entries.end());
                }
            }

            std::string filterLower = g_knowledgeFilter;
            std::transform(filterLower.begin(), filterLower.end(), filterLower.begin(), ::tolower);

            if (ImGui::BeginTable("KnowledgeTable", 4,
                ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                ImGuiTableFlags_ScrollY | ImGuiTableFlags_Resizable)) {

                ImGui::TableSetupScrollFreeze(0, 1);
                ImGui::TableSetupColumn("Key", ImGuiTableColumnFlags_WidthFixed, 70);
                ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthStretch);
                ImGui::TableSetupColumn("Status", ImGuiTableColumnFlags_WidthFixed, 80);
                ImGui::TableSetupColumn("Action", ImGuiTableColumnFlags_WidthFixed, 80);
                ImGui::TableHeadersRow();

                for (int ek : entries_to_show) {
                    bool isLearned = g_state.learned_keys.count(ek) > 0;

                    // Apply filters
                    if (g_showFilter == 1 && !isLearned) continue;
                    if (g_showFilter == 2 && isLearned) continue;

                    auto eit = g_data.entries.find(ek);
                    std::string display = eit != g_data.entries.end() ? eit->second.display : std::to_string(ek);

                    if (filterLower.length() > 0) {
                        std::string dLower = display;
                        std::transform(dLower.begin(), dLower.end(), dLower.begin(), ::tolower);
                        if (dLower.find(filterLower) == std::string::npos) continue;
                    }

                    ImGui::TableNextRow();
                    ImGui::TableNextColumn();
                    ImGui::Text("%d", ek);
                    ImGui::TableNextColumn();
                    ImGui::Text("%s", display.c_str());
                    ImGui::TableNextColumn();
                    if (isLearned) {
                        auto& ks = g_state.learned[ek];
                        ImGui::TextColored(ImVec4(0.2f, 0.9f, 0.2f, 1), "Lv.%d", ks.level);
                    } else {
                        ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "---");
                    }
                    ImGui::TableNextColumn();
                    ImGui::PushID(ek);
                    if (isLearned) {
                        if (ImGui::SmallButton("Unlearn")) {
                            if (UnlearnKnowledge(tree, ek)) dirty = true;
                        }
                    } else {
                        if (ImGui::SmallButton("Learn")) {
                            if (LearnKnowledge(tree, ek, 1)) dirty = true;
                        }
                    }
                    ImGui::PopID();
                }
                ImGui::EndTable();
            }
        }
    } else {
        ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "Select a group from the tree.");

        // Show search results across all entries if filter is active
        if (strlen(g_knowledgeFilter) > 2) {
            std::string filterLower = g_knowledgeFilter;
            std::transform(filterLower.begin(), filterLower.end(), filterLower.begin(), ::tolower);
            ImGui::Separator();
            ImGui::Text("Search results:");
            int shown = 0;
            for (auto& [ek, def] : g_data.entries) {
                std::string dLower = def.display;
                std::transform(dLower.begin(), dLower.end(), dLower.begin(), ::tolower);
                if (dLower.find(filterLower) == std::string::npos) continue;

                bool isLearned = g_state.learned_keys.count(ek) > 0;
                if (g_showFilter == 1 && !isLearned) continue;
                if (g_showFilter == 2 && isLearned) continue;

                ImGui::PushID(ek);
                ImGui::Text("%d", ek);
                ImGui::SameLine(80);
                ImGui::Text("%s", def.display.c_str());
                ImGui::SameLine(350);
                if (isLearned) {
                    ImGui::TextColored(ImVec4(0.2f, 0.9f, 0.2f, 1), "[Learned]");
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Unlearn")) {
                        if (UnlearnKnowledge(tree, ek)) dirty = true;
                    }
                } else {
                    if (ImGui::SmallButton("Learn")) {
                        if (LearnKnowledge(tree, ek, 1)) dirty = true;
                    }
                }
                ImGui::PopID();
                if (++shown >= 50) { ImGui::Text("..."); break; }
            }
        }
    }
    ImGui::EndChild();
}

} // namespace KnowledgeEditor
