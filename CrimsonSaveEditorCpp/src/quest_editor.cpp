#include "quest_editor.h"
#include "imgui.h"
#include <fstream>
#include <algorithm>
#include <cstring>
#include <regex>

namespace QuestEditor {

static const ImVec4 kStateColors[] = {
    {0.5f, 0.5f, 0.5f, 1.0f},  // Unknown - grey
    {0.6f, 0.3f, 0.3f, 1.0f},  // Locked - dark red
    {0.3f, 0.6f, 1.0f, 1.0f},  // Available - blue
    {1.0f, 0.8f, 0.2f, 1.0f},  // InProgress - yellow
    {0.8f, 0.6f, 0.2f, 1.0f},  // CompletionReady - orange
    {0.2f, 0.9f, 0.2f, 1.0f},  // Completed - green
    {1.0f, 0.84f, 0.0f, 1.0f}, // RewardReceived - gold
};
static ImVec4 GetStateColor(uint8_t s) { return (s < 7) ? kStateColors[s] : kStateColors[0]; }

static QuestGameData g_data;
static QuestSaveState g_state;

// ── Data Loading ──

bool LoadGameData(const std::string& json_path) {
    std::ifstream f(json_path);
    if (!f.is_open()) return false;

    json j;
    try { j = json::parse(f); }
    catch (...) { return false; }

    g_data = QuestGameData{};

    // Groups
    for (auto& g : j["groups"]) {
        QuestGroup grp;
        grp.key = g["key"];
        grp.name = g.value("name", "");
        grp.display = g.value("display", "");
        grp.type = g.value("type", 0);
        grp.visible = g.value("visible", true);
        grp.order = g.value("order", 0);
        for (auto& qk : g["quests"]) grp.quests.push_back(qk.get<int>());
        g_data.groups.push_back(std::move(grp));
    }

    // Quests
    for (auto& [k, v] : j["quests"].items()) {
        QuestDef q;
        q.key = (int)std::stoul(k);
        q.name = v.value("name", "");
        q.display = v.value("display", "");
        q.category = v.value("category", 0);
        q.category_name = v.value("category_name", "Side");
        q.group = v.value("group", 0);
        if (v.contains("missions"))
            for (auto& mk : v["missions"]) q.missions.push_back(mk.get<int>());
        if (v.contains("stages"))
            for (auto& sk : v["stages"]) q.stages.push_back(sk.get<int>());
        if (v.contains("gauges"))
            for (auto& gk : v["gauges"]) q.gauges.push_back(gk.get<int>());
        g_data.quests[q.key] = std::move(q);
    }

    // Missions
    for (auto& [k, v] : j["missions"].items()) {
        MissionDef m;
        m.key = (int)std::stoul(k);
        m.name = v.value("name", "");
        m.display = v.value("display", "");
        m.parent_quest = v.value("parent_quest", 0);
        if (v.contains("stages"))
            for (auto& sk : v["stages"]) m.stages.push_back(sk.get<int>());
        if (v.contains("sub_missions"))
            for (auto& sm : v["sub_missions"]) m.sub_missions.push_back(sm.get<int>());
        g_data.missions[m.key] = std::move(m);
    }

    // Stages (large — 51K entries, only store key + parent + conditions)
    for (auto& [k, v] : j["stages"].items()) {
        StageDef s;
        s.key = (int)std::stoul(k);
        s.name = v.value("name", "");
        s.parent_quest = v.value("parent_quest", 0);
        s.owner_mission = v.value("owner_mission", 0);
        s.play_condition = v.value("play_condition", 0);
        s.close_condition = v.value("close_condition", 0);
        g_data.stages[s.key] = std::move(s);
    }

    // Gauges
    for (auto& [k, v] : j["gauges"].items()) {
        GaugeDef g;
        g.key = (int)std::stoul(k);
        g.name = v.value("name", "");
        g.parent_quest = v.value("parent_quest", 0);
        g.percent = v.value("percent", 0);
        g_data.gauges[g.key] = std::move(g);
    }

    // Conditions
    for (auto& [k, v] : j["conditions"].items()) {
        ConditionDef c;
        c.key = (int)std::stoul(k);
        c.string_key = v.value("string_key", "");
        c.expression = v.value("expression", "");
        g_data.conditions[c.key] = std::move(c);
    }

    // Chains
    for (auto& [k, v] : j["chains"].items()) {
        ChainDef ch;
        ch.key = (int)std::stoul(k);
        ch.name = v.value("name", "");
        if (v.contains("sub_quest_keys"))
            for (auto& sk : v["sub_quest_keys"]) ch.sub_quest_keys.push_back(sk.get<int>());
        g_data.chains[ch.key] = std::move(ch);
    }

    // Build synthetic groups for ungrouped quests (camp provisions, factions, etc.)
    std::unordered_set<int> groupedQuests;
    for (auto& grp : g_data.groups)
        for (int qk : grp.quests) groupedQuests.insert(qk);

    std::unordered_map<std::string, std::vector<int>> ungroupedByCat;
    for (auto& [qk, qdef] : g_data.quests) {
        if (groupedQuests.count(qk)) continue;
        // Skip internal/system quests
        if (qdef.display.empty() || qdef.display.find("LevelSequencer") == 0 ||
            qdef.display.find("Func_") == 0 || qdef.display.find("Spawn ") == 0)
            continue;
        std::string cat = qdef.category_name.empty() ? "Side" : qdef.category_name;
        ungroupedByCat[cat].push_back(qk);
    }

    int syntheticKey = 90000;
    for (auto& [cat, qkeys] : ungroupedByCat) {
        if (qkeys.empty()) continue;
        std::sort(qkeys.begin(), qkeys.end());
        QuestGroup grp;
        grp.key = syntheticKey++;
        grp.name = "Ungrouped_" + cat;
        grp.display = cat + " Quests (" + std::to_string(qkeys.size()) + ")";
        grp.type = 99;
        grp.visible = true;
        grp.order = 9000;
        grp.quests = std::move(qkeys);
        g_data.groups.push_back(std::move(grp));
    }

    g_data.loaded = true;
    return true;
}

// ── Save State Scanning ──

static uint8_t ReadU8(const std::vector<uint8_t>& blob, uint32_t off) {
    return (off < blob.size()) ? blob[off] : 0;
}
static uint32_t ReadU32(const std::vector<uint8_t>& blob, uint32_t off) {
    if (off + 4 > blob.size()) return 0;
    return blob[off] | (blob[off+1]<<8) | (blob[off+2]<<16) | (blob[off+3]<<24);
}
static uint64_t ReadU64(const std::vector<uint8_t>& blob, uint32_t off) {
    if (off + 8 > blob.size()) return 0;
    uint64_t lo = ReadU32(blob, off);
    uint64_t hi = ReadU32(blob, off + 4);
    return lo | (hi << 32);
}
static float ReadF32(const std::vector<uint8_t>& blob, uint32_t off) {
    uint32_t bits = ReadU32(blob, off);
    float f;
    memcpy(&f, &bits, 4);
    return f;
}

void ScanSaveState(ParcEngine::SaveTree& tree) {
    g_state = QuestSaveState{};

    for (auto& obj : tree.parsed.objects) {
        if (obj.class_name != "QuestSaveData") continue;

        for (auto& field : obj.fields) {
            if (!field.present) continue;

            if (field.name == "_questStateList") {
                for (auto& elem : field.list_elements) {
                    QuestState qs;
                    for (auto& cf : elem.child_fields) {
                        if (!cf.present) continue;
                        uint32_t sz = cf.end_offset - cf.start_offset;
                        if (cf.name == "_questKey" && sz >= 4)
                            qs.key = (int)ReadU32(tree.blob, cf.start_offset);
                        else if (cf.name == "_state" && sz >= 1) {
                            qs.state = ReadU8(tree.blob, cf.start_offset);
                            qs.state_offset = cf.start_offset;
                        }
                        else if (cf.name == "_completedTime" && sz >= 8) {
                            qs.completed_time = ReadU64(tree.blob, cf.start_offset);
                            qs.completed_time_offset = cf.start_offset;
                            if (qs.completed_time > g_state.max_completed_time)
                                g_state.max_completed_time = qs.completed_time;
                        }
                        else if (cf.name == "_branchedTime" && sz >= 8) {
                            qs.branched_time = ReadU64(tree.blob, cf.start_offset);
                            qs.branched_time_offset = cf.start_offset;
                        }
                    }
                    if (qs.key) g_state.quests[qs.key] = qs;
                }
            }
            else if (field.name == "_missionStateList") {
                for (auto& elem : field.list_elements) {
                    MissionState ms;
                    for (auto& cf : elem.child_fields) {
                        if (!cf.present) continue;
                        uint32_t sz = cf.end_offset - cf.start_offset;
                        if (cf.name == "_key" && sz >= 4)
                            ms.key = (int)ReadU32(tree.blob, cf.start_offset);
                        else if (cf.name == "_state" && sz >= 1) {
                            ms.state = ReadU8(tree.blob, cf.start_offset);
                            ms.state_offset = cf.start_offset;
                        }
                        else if (cf.name == "_completedTime" && sz >= 8) {
                            ms.completed_time = ReadU64(tree.blob, cf.start_offset);
                            ms.completed_time_offset = cf.start_offset;
                            if (ms.completed_time > g_state.max_completed_time)
                                g_state.max_completed_time = ms.completed_time;
                        }
                        else if (cf.name == "_completeCount" && sz >= 4) {
                            ms.complete_count = (int)ReadU32(tree.blob, cf.start_offset);
                            ms.complete_count_offset = cf.start_offset;
                        }
                    }
                    if (ms.key) g_state.missions[ms.key] = ms;
                }
            }
            else if (field.name == "_stageStateData") {
                for (auto& elem : field.list_elements) {
                    StageState ss;
                    for (auto& cf : elem.child_fields) {
                        if (!cf.present) continue;
                        uint32_t sz = cf.end_offset - cf.start_offset;
                        if (cf.name == "_key" && sz >= 4)
                            ss.key = (int)ReadU32(tree.blob, cf.start_offset);
                        else if (cf.name == "_state" && sz >= 1) {
                            ss.state = ReadU8(tree.blob, cf.start_offset);
                            ss.state_offset = cf.start_offset;
                        }
                        else if (cf.name == "_completedTime" && sz >= 8) {
                            ss.completed_time = ReadU64(tree.blob, cf.start_offset);
                            ss.completed_time_offset = cf.start_offset;
                        }
                        else if (cf.name == "_completedCount" && sz >= 4) {
                            ss.completed_count = (int)ReadU32(tree.blob, cf.start_offset);
                            ss.completed_count_offset = cf.start_offset;
                        }
                    }
                    if (ss.key) g_state.stages[ss.key] = ss;
                }
            }
            else if (field.name == "_questGaugeStateList") {
                for (auto& elem : field.list_elements) {
                    GaugeState gs;
                    for (auto& cf : elem.child_fields) {
                        if (!cf.present) continue;
                        uint32_t sz = cf.end_offset - cf.start_offset;
                        if (cf.name == "_key" && sz >= 4)
                            gs.key = (int)ReadU32(tree.blob, cf.start_offset);
                        else if (cf.name == "_state" && sz >= 1) {
                            gs.state = ReadU8(tree.blob, cf.start_offset);
                            gs.state_offset = cf.start_offset;
                        }
                        else if (cf.name == "_killRatio" && sz >= 4) {
                            gs.kill_ratio = ReadF32(tree.blob, cf.start_offset);
                            gs.ratio_offset = cf.start_offset;
                        }
                    }
                    if (gs.key) g_state.gauges[gs.key] = gs;
                }
            }
        }
        break;
    }
    g_state.scanned = true;
}

// ── Editing Operations ──

static void WriteU8(std::vector<uint8_t>& blob, uint32_t off, uint8_t val) {
    if (off < blob.size()) blob[off] = val;
}
static void WriteU32(std::vector<uint8_t>& blob, uint32_t off, uint32_t val) {
    if (off + 4 > blob.size()) return;
    memcpy(&blob[off], &val, 4);
}
static void WriteU64(std::vector<uint8_t>& blob, uint32_t off, uint64_t val) {
    if (off + 8 > blob.size()) return;
    memcpy(&blob[off], &val, 8);
}
static void WriteF32(std::vector<uint8_t>& blob, uint32_t off, float val) {
    if (off + 4 > blob.size()) return;
    memcpy(&blob[off], &val, 4);
}

bool CompleteQuest(ParcEngine::SaveTree& tree, int quest_key) {
    auto qit = g_state.quests.find(quest_key);
    if (qit == g_state.quests.end()) return false;

    // Set quest state = Completed
    WriteU8(tree.blob, qit->second.state_offset, STATE_COMPLETED);
    qit->second.state = STATE_COMPLETED;

    // Complete all missions + sub_missions for this quest
    auto qdef = g_data.quests.find(quest_key);
    if (qdef != g_data.quests.end()) {
        for (int mk : qdef->second.missions)
            CompleteMission(tree, mk);

        // Also complete direct quest stages not under any mission
        for (int sk : qdef->second.stages) {
            auto sit = g_state.stages.find(sk);
            if (sit != g_state.stages.end() && sit->second.state < STATE_COMPLETED) {
                WriteU8(tree.blob, sit->second.state_offset, STATE_COMPLETED);
                sit->second.state = STATE_COMPLETED;
            }
        }

        // Set gauges to complete
        for (int gk : qdef->second.gauges) {
            auto git = g_state.gauges.find(gk);
            if (git != g_state.gauges.end()) {
                WriteU8(tree.blob, git->second.state_offset, STATE_COMPLETED);
                git->second.state = STATE_COMPLETED;
                if (git->second.ratio_offset) {
                    WriteF32(tree.blob, git->second.ratio_offset, 1.0f);
                    git->second.kill_ratio = 1.0f;
                }
            }
        }
    }

    return true;
}

bool CompleteMission(ParcEngine::SaveTree& tree, int mission_key) {
    auto mit = g_state.missions.find(mission_key);
    if (mit == g_state.missions.end()) return false;

    WriteU8(tree.blob, mit->second.state_offset, STATE_COMPLETED);
    mit->second.state = STATE_COMPLETED;

    auto mdef = g_data.missions.find(mission_key);
    if (mdef != g_data.missions.end()) {
        // Complete all stages of this mission
        for (int sk : mdef->second.stages) {
            auto sit = g_state.stages.find(sk);
            if (sit != g_state.stages.end() && sit->second.state < STATE_COMPLETED) {
                WriteU8(tree.blob, sit->second.state_offset, STATE_COMPLETED);
                sit->second.state = STATE_COMPLETED;
            }
        }
        // Complete all sub_missions recursively
        for (int smk : mdef->second.sub_missions)
            CompleteMission(tree, smk);
    }
    return true;
}

bool CompleteStage(ParcEngine::SaveTree& tree, int stage_key) {
    auto sit = g_state.stages.find(stage_key);
    if (sit == g_state.stages.end()) return false;
    WriteU8(tree.blob, sit->second.state_offset, STATE_COMPLETED);
    sit->second.state = STATE_COMPLETED;
    return true;
}

bool ResetQuest(ParcEngine::SaveTree& tree, int quest_key) {
    auto qit = g_state.quests.find(quest_key);
    if (qit == g_state.quests.end()) return false;

    // Reset quest state + zero timestamps
    WriteU8(tree.blob, qit->second.state_offset, STATE_AVAILABLE);
    qit->second.state = STATE_AVAILABLE;
    if (qit->second.completed_time_offset) {
        WriteU64(tree.blob, qit->second.completed_time_offset, 0);
        qit->second.completed_time = 0;
    }
    if (qit->second.branched_time_offset) {
        WriteU64(tree.blob, qit->second.branched_time_offset, 0);
        qit->second.branched_time = 0;
    }

    auto qdef = g_data.quests.find(quest_key);
    if (qdef != g_data.quests.end()) {
        // Reset all missions (including sub_missions)
        std::function<void(int)> resetMission = [&](int mk) {
            auto mit = g_state.missions.find(mk);
            if (mit != g_state.missions.end()) {
                WriteU8(tree.blob, mit->second.state_offset, STATE_AVAILABLE);
                mit->second.state = STATE_AVAILABLE;
                if (mit->second.completed_time_offset) {
                    WriteU64(tree.blob, mit->second.completed_time_offset, 0);
                    mit->second.completed_time = 0;
                }
                if (mit->second.complete_count_offset) {
                    WriteU32(tree.blob, mit->second.complete_count_offset, 0);
                    mit->second.complete_count = 0;
                }
            }
            // Also reset sub_missions recursively
            auto mdef = g_data.missions.find(mk);
            if (mdef != g_data.missions.end()) {
                for (int smk : mdef->second.sub_missions)
                    resetMission(smk);
                for (int sk : mdef->second.stages) {
                    auto sit = g_state.stages.find(sk);
                    if (sit != g_state.stages.end()) {
                        WriteU8(tree.blob, sit->second.state_offset, STATE_AVAILABLE);
                        sit->second.state = STATE_AVAILABLE;
                        if (sit->second.completed_time_offset) {
                            WriteU64(tree.blob, sit->second.completed_time_offset, 0);
                            sit->second.completed_time = 0;
                        }
                        if (sit->second.completed_count_offset) {
                            WriteU32(tree.blob, sit->second.completed_count_offset, 0);
                            sit->second.completed_count = 0;
                        }
                    }
                }
            }
        };

        for (int mk : qdef->second.missions)
            resetMission(mk);

        // Reset direct quest stages
        for (int sk : qdef->second.stages) {
            auto sit = g_state.stages.find(sk);
            if (sit != g_state.stages.end()) {
                WriteU8(tree.blob, sit->second.state_offset, STATE_AVAILABLE);
                sit->second.state = STATE_AVAILABLE;
                if (sit->second.completed_time_offset) {
                    WriteU64(tree.blob, sit->second.completed_time_offset, 0);
                    sit->second.completed_time = 0;
                }
                if (sit->second.completed_count_offset) {
                    WriteU32(tree.blob, sit->second.completed_count_offset, 0);
                    sit->second.completed_count = 0;
                }
            }
        }

        // Reset gauges
        for (int gk : qdef->second.gauges) {
            auto git = g_state.gauges.find(gk);
            if (git != g_state.gauges.end()) {
                WriteU8(tree.blob, git->second.state_offset, STATE_AVAILABLE);
                git->second.state = STATE_AVAILABLE;
                if (git->second.ratio_offset) {
                    WriteF32(tree.blob, git->second.ratio_offset, 0.0f);
                    git->second.kill_ratio = 0.0f;
                }
            }
        }
    }
    return true;
}

bool CompleteChapter(ParcEngine::SaveTree& tree, int group_key) {
    for (auto& grp : g_data.groups) {
        if (grp.key != group_key) continue;
        for (int qk : grp.quests)
            CompleteQuest(tree, qk);
        return true;
    }
    return false;
}

// ── Condition Analysis ──

std::string GetConditionWarning(int quest_key) {
    auto qdef = g_data.quests.find(quest_key);
    if (qdef == g_data.quests.end()) return "";

    std::string warnings;
    // Check if any stages of this quest have play_conditions with unmet prerequisites
    for (int mk : qdef->second.missions) {
        auto mdef = g_data.missions.find(mk);
        if (mdef == g_data.missions.end()) continue;
        for (int sk : mdef->second.stages) {
            auto sdef = g_data.stages.find(sk);
            if (sdef == g_data.stages.end()) continue;
            if (sdef->second.play_condition) {
                auto cit = g_data.conditions.find(sdef->second.play_condition);
                if (cit != g_data.conditions.end()) {
                    // Check if referenced quests/missions are completed
                    std::string expr = cit->second.expression;
                    std::regex re_quest("CompleteQuest\\(([^)]+)\\)");
                    std::regex re_mission("CompleteMission\\(([^)]+)\\)");
                    std::smatch match;
                    if (std::regex_search(expr, match, re_quest)) {
                        if (warnings.empty()) warnings = "Conditions: ";
                        warnings += expr.substr(0, 80) + "\n";
                        break;
                    }
                    if (std::regex_search(expr, match, re_mission)) {
                        if (warnings.empty()) warnings = "Conditions: ";
                        warnings += expr.substr(0, 80) + "\n";
                        break;
                    }
                }
            }
        }
        if (!warnings.empty()) break;
    }
    return warnings;
}

std::vector<int> GetUnlockedBy(int quest_key) {
    // Find quests/stages that have conditions referencing this quest
    std::vector<int> unlocked;
    auto qdef = g_data.quests.find(quest_key);
    if (qdef == g_data.quests.end()) return unlocked;

    std::string quest_name = qdef->second.name;
    for (auto& [ck, cond] : g_data.conditions) {
        if (cond.expression.find(quest_name) != std::string::npos) {
            // Find which stages use this condition
            for (auto& [sk, sdef] : g_data.stages) {
                if (sdef.play_condition == ck && sdef.parent_quest != quest_key) {
                    unlocked.push_back(sdef.parent_quest);
                    break;
                }
            }
        }
    }
    return unlocked;
}

const QuestGameData& GetGameData() { return g_data; }
const QuestSaveState& GetSaveState() { return g_state; }

// ── UI Rendering ──

static int g_selectedGroup = -1;
static int g_selectedQuest = -1;
static char g_questStatus[256] = {};
static char g_questFilter[128] = {};
static int g_stateFilter = -1; // -1 = all

static uint8_t GetQuestSaveState(int key) {
    auto it = g_state.quests.find(key);
    return (it != g_state.quests.end()) ? it->second.state : 0;
}
static uint8_t GetMissionSaveState(int key) {
    auto it = g_state.missions.find(key);
    return (it != g_state.missions.end()) ? it->second.state : 0;
}
static uint8_t GetStageSaveState(int key) {
    auto it = g_state.stages.find(key);
    return (it != g_state.stages.end()) ? it->second.state : 0;
}

static float GetGroupCompletion(const QuestGroup& grp) {
    if (grp.quests.empty()) return 0.0f;
    int done = 0;
    for (int qk : grp.quests) {
        uint8_t s = GetQuestSaveState(qk);
        if (s >= STATE_COMPLETED) done++;
    }
    return (float)done / (float)grp.quests.size();
}

void RenderQuestTab(ParcEngine::SaveTree& tree, bool& dirty) {
    if (!g_data.loaded) {
        ImGui::TextColored(ImVec4(1, 0.4f, 0.3f, 1), "Quest data not loaded. Place quest_game_data.json in data/");
        return;
    }
    if (!g_state.scanned) {
        ImGui::TextColored(ImVec4(1, 0.8f, 0.3f, 1), "Load a save to view quest states.");
        return;
    }

    // Top bar
    ImGui::SetNextItemWidth(200);
    ImGui::InputText("Filter", g_questFilter, sizeof(g_questFilter));
    ImGui::SameLine();
    ImGui::SetNextItemWidth(120);
    const char* stateFilterNames[] = {"All", "Locked", "Available", "InProgress", "Completed", "Reward"};
    if (ImGui::BeginCombo("State##filter", g_stateFilter < 0 ? "All" : stateFilterNames[g_stateFilter])) {
        if (ImGui::Selectable("All", g_stateFilter < 0)) g_stateFilter = -1;
        for (int i = 1; i <= 5; i++)
            if (ImGui::Selectable(stateFilterNames[i], g_stateFilter == i)) g_stateFilter = i;
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    ImGui::TextColored(ImVec4(0.6f, 0.6f, 0.6f, 1), "Quests: %d | Missions: %d | Stages: %d",
        (int)g_state.quests.size(), (int)g_state.missions.size(), (int)g_state.stages.size());

    if (g_questStatus[0]) {
        ImGui::TextColored(ImVec4(0.3f, 1.0f, 0.4f, 1), "%s", g_questStatus);
    }

    // Three-panel layout
    float panelWidth = ImGui::GetContentRegionAvail().x;
    float leftW = panelWidth * 0.22f;
    float midW = panelWidth * 0.33f;
    float rightW = panelWidth - leftW - midW - 16;

    // LEFT: Quest Groups
    ImGui::BeginChild("QuestGroups", ImVec2(leftW, 0), true);
    ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1), "Quest Groups");
    ImGui::Separator();

    for (int gi = 0; gi < (int)g_data.groups.size(); gi++) {
        auto& grp = g_data.groups[gi];
        if (!grp.visible) continue;

        float completion = GetGroupCompletion(grp);
        const char* icon = (completion >= 1.0f) ? "[+]" : (completion > 0) ? "[~]" : "[ ]";

        char label[256];
        snprintf(label, sizeof(label), "%s %s (%d)", icon, grp.display.c_str(), (int)grp.quests.size());

        bool selected = (g_selectedGroup == gi);
        if (ImGui::Selectable(label, selected)) {
            g_selectedGroup = gi;
            g_selectedQuest = -1;
        }
    }
    ImGui::EndChild();

    ImGui::SameLine();

    // MIDDLE: Quests in selected group
    ImGui::BeginChild("QuestList", ImVec2(midW, 0), true);
    ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1), "Quests");
    ImGui::Separator();

    if (g_selectedGroup >= 0 && g_selectedGroup < (int)g_data.groups.size()) {
        auto& grp = g_data.groups[g_selectedGroup];
        std::string filterLower = g_questFilter;
        std::transform(filterLower.begin(), filterLower.end(), filterLower.begin(), ::tolower);

        for (int qi = 0; qi < (int)grp.quests.size(); qi++) {
            int qk = grp.quests[qi];
            auto qit = g_data.quests.find(qk);
            if (qit == g_data.quests.end()) continue;

            uint8_t state = GetQuestSaveState(qk);

            // Filter by state
            if (g_stateFilter > 0 && state != g_stateFilter) continue;

            // Filter by name
            if (filterLower.length() > 0) {
                std::string dispLower = qit->second.display;
                std::transform(dispLower.begin(), dispLower.end(), dispLower.begin(), ::tolower);
                if (dispLower.find(filterLower) == std::string::npos) continue;
            }

            ImVec4 color = GetStateColor(state);
            const char* stateStr = (state < 7) ? GetStateName(state) : "?";

            char qlabel[512];
            snprintf(qlabel, sizeof(qlabel), "[%s] %s##q%d", stateStr, qit->second.display.c_str(), qk);
            ImGui::PushStyleColor(ImGuiCol_Text, color);
            if (ImGui::Selectable(qlabel, g_selectedQuest == qk))
                g_selectedQuest = qk;
            ImGui::PopStyleColor();
        }

        // Batch operations
        ImGui::Separator();
        if (ImGui::SmallButton("Complete Chapter")) {
            if (CompleteChapter(tree, grp.key)) {
                dirty = true;
                snprintf(g_questStatus, sizeof(g_questStatus), "Completed chapter: %s", grp.display.c_str());
            } else {
                snprintf(g_questStatus, sizeof(g_questStatus), "Chapter already complete or quests not in save");
            }
        }
    }
    ImGui::EndChild();

    ImGui::SameLine();

    // RIGHT: Quest details + actions
    ImGui::BeginChild("QuestDetails", ImVec2(rightW, 0), true);
    if (g_selectedQuest > 0) {
        auto qit = g_data.quests.find(g_selectedQuest);
        if (qit != g_data.quests.end()) {
            auto& qdef = qit->second;
            uint8_t qstate = GetQuestSaveState(g_selectedQuest);

            ImGui::TextColored(ImVec4(1, 0.9f, 0.5f, 1), "%s", qdef.display.c_str());
            ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "%s | %s", qdef.name.c_str(), qdef.category_name.c_str());
            ImGui::TextColored(GetStateColor(qstate), "State: %s", GetStateName(qstate));
            ImGui::Separator();

            // Missions
            ImGui::TextColored(ImVec4(0.5f, 0.8f, 1, 1), "Missions (%d):", (int)qdef.missions.size());

            // Render a mission + its sub_missions recursively
            std::function<void(int, int)> renderMission = [&](int mk, int depth) {
                auto mdef = g_data.missions.find(mk);
                uint8_t ms = GetMissionSaveState(mk);
                std::string mname = mdef != g_data.missions.end() ? mdef->second.display : std::to_string(mk);
                if (mname.empty() && mdef != g_data.missions.end()) mname = mdef->second.name;
                if (mname.empty()) mname = std::to_string(mk);

                std::string indent(depth * 2 + 2, ' ');
                ImGui::TextColored(GetStateColor(ms), "%s%s [%s]", indent.c_str(), mname.c_str(), GetStateName(ms));

                if (mdef != g_data.missions.end()) {
                    // Show stages under mission
                    for (int sk : mdef->second.stages) {
                        uint8_t ss = GetStageSaveState(sk);
                        auto sdef = g_data.stages.find(sk);
                        std::string sname = sdef != g_data.stages.end() ? sdef->second.name : std::to_string(sk);
                        std::string sindent(depth * 2 + 4, ' ');
                        ImGui::TextColored(ImVec4(0.4f, 0.4f, 0.4f, 1), "%s%s [%s]",
                            sindent.c_str(), sname.c_str(), GetStateName(ss));
                    }
                    // Show sub_missions
                    for (int smk : mdef->second.sub_missions)
                        renderMission(smk, depth + 1);
                }
            };

            for (int mk : qdef.missions) {
                // Skip sub_missions at top level (they'll be shown under their parent)
                auto mdef = g_data.missions.find(mk);
                if (mdef != g_data.missions.end()) {
                    bool isSubMission = false;
                    for (int pmk : qdef.missions) {
                        if (pmk == mk) continue;
                        auto pdef = g_data.missions.find(pmk);
                        if (pdef != g_data.missions.end()) {
                            for (int smk : pdef->second.sub_missions) {
                                if (smk == mk) { isSubMission = true; break; }
                            }
                        }
                        if (isSubMission) break;
                    }
                    if (isSubMission) continue;
                }
                renderMission(mk, 0);
            }

            // Condition warnings
            std::string warn = GetConditionWarning(g_selectedQuest);
            if (!warn.empty()) {
                ImGui::Separator();
                ImGui::TextColored(ImVec4(1, 0.6f, 0.2f, 1), "Dependencies:");
                ImGui::TextWrapped("%s", warn.c_str());
            }

            // Actions
            ImGui::Separator();
            ImGui::TextColored(ImVec4(1, 0.7f, 0.3f, 1), "Actions:");
            if (qstate < STATE_COMPLETED) {
                if (ImGui::Button("Complete Quest", ImVec2(200, 0))) {
                    if (CompleteQuest(tree, g_selectedQuest)) {
                        dirty = true;
                        snprintf(g_questStatus, sizeof(g_questStatus), "Completed: %s + %d missions", qdef.display.c_str(), (int)qdef.missions.size());
                    } else {
                        snprintf(g_questStatus, sizeof(g_questStatus), "Failed: quest %d not found in save", g_selectedQuest);
                    }
                }
                ImGui::SameLine();
                ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "(quest + missions + stages + gauges)");
            }
            if (qstate > STATE_LOCKED && qstate != STATE_AVAILABLE) {
                if (ImGui::Button("Reset to Available", ImVec2(200, 0))) {
                    if (ResetQuest(tree, g_selectedQuest)) {
                        dirty = true;
                        snprintf(g_questStatus, sizeof(g_questStatus), "Reset: %s to Available", qdef.display.c_str());
                    } else {
                        snprintf(g_questStatus, sizeof(g_questStatus), "Failed: quest %d not found in save", g_selectedQuest);
                    }
                }
            }

            // Per-mission complete buttons
            ImGui::Separator();
            for (int mk : qdef.missions) {
                uint8_t ms = GetMissionSaveState(mk);
                if (ms < STATE_COMPLETED) {
                    auto mdef = g_data.missions.find(mk);
                    std::string mname = mdef != g_data.missions.end() ? mdef->second.display : std::to_string(mk);
                    char btn[256];
                    snprintf(btn, sizeof(btn), "Complete: %s##m%d", mname.c_str(), mk);
                    if (ImGui::SmallButton(btn)) {
                        if (CompleteMission(tree, mk)) {
                            dirty = true;
                            snprintf(g_questStatus, sizeof(g_questStatus), "Completed mission: %s", mname.c_str());
                        } else {
                            snprintf(g_questStatus, sizeof(g_questStatus), "Failed: mission %d not in save", mk);
                        }
                    }
                }
            }
        }
    } else {
        ImGui::TextColored(ImVec4(0.5f, 0.5f, 0.5f, 1), "Select a quest to view details.");
    }
    ImGui::EndChild();
}

} // namespace QuestEditor
