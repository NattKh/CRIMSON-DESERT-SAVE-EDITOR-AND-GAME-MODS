#pragma once
#include "parc_engine.h"
#include <string>
#include <vector>
#include <unordered_map>
#include <cstdint>

namespace DyeEditor {

struct DyeSlot {
    int8_t  slot = 0;
    uint8_t r = 0, g = 0, b = 0, a = 255;
    int8_t  grime = 0;
    uint32_t color_group = 0;
    uint16_t material = 0;
    uint8_t  mask = 0;
    // Byte offsets for direct editing (0 = field not present in save)
    uint32_t off_slot = 0;
    uint32_t off_r = 0, off_g = 0, off_b = 0, off_a = 0;
    uint32_t off_grime = 0;
    uint32_t off_group = 0;
    uint32_t off_material = 0;
    uint32_t elem_start = 0;
};

struct DyedItem {
    uint32_t item_key = 0;
    std::string item_name;
    std::vector<DyeSlot> slots;
    int equip_index = -1;
};

struct DyePreset {
    std::string name;
    uint8_t r, g, b, a;
    uint32_t color_group;
    uint16_t material;
    int8_t grime;
};

bool ScanDyeData(ParcEngine::SaveTree& tree);
bool InjectDye(ParcEngine::SaveTree& tree, int equip_index, int num_slots,
               uint8_t r, uint8_t g, uint8_t b, uint16_t material, uint32_t color_group);
void RenderDyeTab(ParcEngine::SaveTree& tree, bool& dirty);

const std::vector<DyedItem>& GetDyedItems();

} // namespace DyeEditor
