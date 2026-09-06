#pragma once
#include "parc_engine.h"
#include <string>
#include <vector>
#include <unordered_map>
#include <cstdint>

namespace WorldEditor {

struct FactionEntry {
    uint32_t key = 0;
    uint32_t leader_char = 0;
    uint16_t relation_group = 0;
    uint32_t key_offset = 0;
    uint32_t relation_offset = 0;
    uint32_t leader_offset = 0;
    int relation_count = 0;
};

struct FactionNode {
    uint32_t owner_faction = 0;
    uint8_t  faction_state = 0;
    uint32_t conqueror = 0;
    uint8_t  is_capital = 0;
    uint8_t  block_sub_type = 0;
    uint8_t  operation_state = 0;
    uint8_t  enable_node = 0;
    uint32_t state_offset = 0;
    uint32_t conqueror_offset = 0;
    uint32_t enable_offset = 0;
    uint32_t operation_offset = 0;
};

struct FriendEntry {
    uint32_t character_key = 0;
    uint32_t char_key_offset = 0;
    uint32_t level = 0;
    uint64_t exp = 0;
    uint32_t level_offset = 0;
    uint32_t exp_offset = 0;
};

struct FieldNPC {
    uint32_t spawn_field = 0;
    uint32_t npc_key = 0;
    uint32_t character_key = 0;
    uint64_t touch_id = 0;
    uint32_t char_key_offset = 0;
    uint32_t touch_offset = 0;
    uint32_t friendly_level = 0;
    uint64_t friendly_exp = 0;
    uint32_t friendly_level_offset = 0;
    uint32_t friendly_exp_offset = 0;
};

struct SubLevelEntry {
    uint32_t key = 0;
    uint32_t max_level = 0;
    uint32_t level = 0;
    uint64_t experience = 0;
    uint32_t level_offset = 0;
    uint32_t max_level_offset = 0;
    uint32_t exp_offset = 0;
};

struct MercEntry {
    uint32_t character_key = 0;
    uint64_t merc_no = 0;
    uint8_t  last_summoned = 0;
    uint8_t  is_init = 0;
    uint64_t current_hp = 0;
    uint64_t current_mp = 0;
    int equip_count = 0;
    int inv_count = 0;
    uint32_t hp_offset = 0;
    uint32_t mp_offset = 0;
    uint32_t summoned_offset = 0;
};

struct StoreItem {
    uint64_t trade_count = 0;
    uint32_t trade_offset = 0;
};

struct StoreEntry {
    uint16_t store_key = 0;
    uint64_t last_refresh = 0;
    uint32_t refresh_offset = 0;
    std::vector<StoreItem> items;
};

struct WaypointEntry {
    uint32_t key = 0;
    float pos_x = 0, pos_y = 0, pos_z = 0;
    uint32_t key_offset = 0;
    int elem_index = -1;
};

struct WorldState {
    std::vector<FactionEntry> factions;
    std::vector<FactionNode> faction_nodes;
    std::vector<FriendEntry> friendships;
    std::vector<FieldNPC> field_npcs;
    std::vector<SubLevelEntry> sublevels;
    std::vector<MercEntry> mercenaries;
    std::vector<StoreEntry> stores;
    std::vector<WaypointEntry> waypoints;

    // ContentsMisc
    uint16_t housing_region = 0;
    uint64_t timewarp_cooldown = 0;
    int pin_count = 0;
    int alert_count = 0;
    uint32_t housing_offset = 0;
    uint32_t timewarp_offset = 0;

    // GamePlayVariable
    struct GameVar { uint32_t key=0; uint8_t value=0; uint32_t val_offset=0; };
    std::vector<GameVar> game_vars;

    // RoyalSupply
    struct RoyalSupply { uint16_t key=0; uint32_t item_key=0; uint64_t remain=0; uint32_t remain_offset=0; };
    std::vector<RoyalSupply> royal_supplies;

    // InventoryItemContents
    uint8_t investment_propensity = 0;
    uint32_t investment_offset = 0;

    bool scanned = false;
};

void ScanAll(ParcEngine::SaveTree& tree);
void RenderWorldTab(ParcEngine::SaveTree& tree, bool& dirty);
void RenderStoresTab(ParcEngine::SaveTree& tree, bool& dirty);
void RenderWaypointsTab(ParcEngine::SaveTree& tree, bool& dirty);
void RenderMiscTab(ParcEngine::SaveTree& tree, bool& dirty);

} // namespace WorldEditor
