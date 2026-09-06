#include "editor_common.h"

// GetItemDisplayName is defined in inventory_editor.cpp
extern std::string GetItemDisplayName(uint32_t key);

namespace EditorCommon {

std::string GetItemName(uint32_t key) {
    std::string n = GetItemDisplayName(key);
    if (!n.empty()) return n;
    return "ID:" + std::to_string(key);
}

} // namespace EditorCommon
