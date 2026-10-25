#pragma once
#include "parc_engine.h"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <cstdint>

namespace KnowledgeEditor {

using json = nlohmann::json;

struct KnowledgeGroup {
    int key = 0;
    std::string name;
    std::string display;
    int parent = 0;
    bool show_ui = true;
    bool meditation_learnable = false;
    std::vector<int> children;
    std::vector<int> knowledge_entries;
};

struct KnowledgeDef {
    int key = 0;
    std::string name;
    std::string display;
    std::string mask_hex;
};

struct KnowledgeState {
    int key = 0;
    int level = 0;
    uint32_t key_offset = 0;
    uint32_t level_offset = 0;
    int element_index = -1;
};

struct KnowledgeGameData {
    std::vector<KnowledgeGroup> top_groups;
    std::unordered_map<int, KnowledgeGroup> groups;
    std::unordered_map<int, KnowledgeDef> entries;
    std::unordered_map<int, std::string> masks;
    bool loaded = false;
};

struct KnowledgeSaveState {
    std::unordered_map<int, KnowledgeState> learned;
    std::unordered_set<int> learned_keys;
    bool scanned = false;
};

bool LoadGameData(const std::string& keys_path, const std::string& community_path, const std::string& groups_path);
void ScanSaveState(ParcEngine::SaveTree& tree);
void RenderKnowledgeTab(ParcEngine::SaveTree& tree, bool& dirty);

bool LearnKnowledge(ParcEngine::SaveTree& tree, int key, int level = 1);
bool UnlearnKnowledge(ParcEngine::SaveTree& tree, int key);
int LearnAllInGroup(ParcEngine::SaveTree& tree, int group_key);
int LearnAllAbyss(ParcEngine::SaveTree& tree);
int LearnDragonRiding(ParcEngine::SaveTree& tree);
int LearnAll(ParcEngine::SaveTree& tree);

const KnowledgeGameData& GetGameData();
const KnowledgeSaveState& GetSaveState();

} // namespace KnowledgeEditor
