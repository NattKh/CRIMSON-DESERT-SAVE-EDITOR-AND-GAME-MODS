#pragma once
#include "parc_engine.h"
#include <string>
#include <vector>
#include <cstdint>

namespace AppearanceEditor {

struct AppearanceData {
    std::vector<uint8_t> mesh_data;      // 16 bytes of mesh/preset values
    std::vector<uint8_t> decoration_data; // 250 bytes of decoration values
    uint32_t version = 0;
    uint32_t mesh_offset = 0;       // blob offset of the raw array data (after prefix+count)
    uint32_t decoration_offset = 0;
    uint32_t version_offset = 0;
    uint32_t mesh_count_offset = 0; // offset of the u32 count before data
    uint32_t deco_count_offset = 0;
    std::string owner_name;
    uint32_t owner_key = 0;
    bool is_player = false;
};

struct AppearanceState {
    AppearanceData player;
    std::vector<AppearanceData> mercenaries;
    bool scanned = false;
};

void ScanAppearance(ParcEngine::SaveTree& tree);
void RenderAppearanceTab(ParcEngine::SaveTree& tree, bool& dirty);

} // namespace AppearanceEditor
