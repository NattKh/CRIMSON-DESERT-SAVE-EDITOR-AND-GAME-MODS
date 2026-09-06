#pragma once
#include "parc_engine.h"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <cstdint>

namespace QuestEditor {

using json = nlohmann::json;

// ── Game Data Structs ──

struct QuestGroup {
    int key = 0;
    std::string name, display;
    int type = 0;
    bool visible = true;
    int order = 0;
    std::vector<int> quests;
};

struct QuestDef {
    int key = 0;
    std::string name, display, category_name;
    int category = 0;
    int group = 0;
    std::vector<int> missions;
    std::vector<int> stages;
    std::vector<int> gauges;
};

struct MissionDef {
    int key = 0;
    std::string name, display;
    int parent_quest = 0;
    std::vector<int> stages;
    std::vector<int> sub_missions;
};

struct StageDef {
    int key = 0;
    std::string name;
    int parent_quest = 0;
    int owner_mission = 0;
    int play_condition = 0;
    int close_condition = 0;
};

struct GaugeDef {
    int key = 0;
    std::string name;
    int parent_quest = 0;
    int percent = 0;
};

struct ConditionDef {
    int key = 0;
    std::string string_key;
    std::string expression;
};

struct ChainDef {
    int key = 0;
    std::string name;
    std::vector<int> sub_quest_keys;
};

// ── Save State Structs ──

struct QuestState {
    int key = 0;
    uint8_t state = 0;
    uint32_t state_offset = 0;
    uint64_t completed_time = 0;
    uint32_t completed_time_offset = 0;
    uint64_t branched_time = 0;
    uint32_t branched_time_offset = 0;
};

struct MissionState {
    int key = 0;
    uint8_t state = 0;
    uint32_t state_offset = 0;
    uint64_t completed_time = 0;
    uint32_t completed_time_offset = 0;
    int complete_count = 0;
    uint32_t complete_count_offset = 0;
};

struct StageState {
    int key = 0;
    uint8_t state = 0;
    uint32_t state_offset = 0;
    uint64_t completed_time = 0;
    uint32_t completed_time_offset = 0;
    int completed_count = 0;
    uint32_t completed_count_offset = 0;
};

struct GaugeState {
    int key = 0;
    uint8_t state = 0;
    uint32_t state_offset = 0;
    float kill_ratio = 0.0f;
    uint32_t ratio_offset = 0;
};

// ── Enums ──

enum QuestStateEnum : uint8_t {
    STATE_UNKNOWN = 0,
    STATE_LOCKED = 1,
    STATE_AVAILABLE = 2,
    STATE_IN_PROGRESS = 3,
    STATE_COMPLETION_READY = 4,
    STATE_COMPLETED = 5,
    STATE_REWARD_RECEIVED = 6,
};

static inline const char* GetStateName(uint8_t s) {
    static const char* names[] = {
        "Unknown", "Locked", "Available", "InProgress",
        "CompletionReady", "Completed", "RewardReceived"
    };
    return (s < 7) ? names[s] : "?";
}

// ── Global Data ──

struct QuestGameData {
    std::vector<QuestGroup> groups;
    std::unordered_map<int, QuestDef> quests;
    std::unordered_map<int, MissionDef> missions;
    std::unordered_map<int, StageDef> stages;
    std::unordered_map<int, GaugeDef> gauges;
    std::unordered_map<int, ConditionDef> conditions;
    std::unordered_map<int, ChainDef> chains;

    bool loaded = false;
};

struct QuestSaveState {
    std::unordered_map<int, QuestState> quests;
    std::unordered_map<int, MissionState> missions;
    std::unordered_map<int, StageState> stages;
    std::unordered_map<int, GaugeState> gauges;
    uint64_t max_completed_time = 0;

    bool scanned = false;
};

// ── API ──

bool LoadGameData(const std::string& json_path);
void ScanSaveState(ParcEngine::SaveTree& tree);
void RenderQuestTab(ParcEngine::SaveTree& tree, bool& dirty);

// Editing operations
bool CompleteQuest(ParcEngine::SaveTree& tree, int quest_key);
bool CompleteMission(ParcEngine::SaveTree& tree, int mission_key);
bool CompleteStage(ParcEngine::SaveTree& tree, int stage_key);
bool ResetQuest(ParcEngine::SaveTree& tree, int quest_key);
bool CompleteChapter(ParcEngine::SaveTree& tree, int group_key);

// Access
const QuestGameData& GetGameData();
const QuestSaveState& GetSaveState();
std::string GetConditionWarning(int quest_key);
std::vector<int> GetUnlockedBy(int quest_key);

} // namespace QuestEditor
