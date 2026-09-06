#pragma once
#include <vector>
#include <cstdint>
#include <string>
#include <unordered_map>

namespace ItemFactory {

// Mask patterns — user selects which type
enum class ItemMask {
    Equipment,          // weapon/armor with sockets
    EquipmentNoSocket,  // weapon/armor without sockets
    SimpleConsumable,   // currency/materials: just key + count
    QuestItem,          // quest/key items: key + count + timestamp
};

static const char* ItemMaskNames[] = {
    "Equipment (with sockets)",
    "Equipment (no sockets)",
    "Consumable / Material",
    "Quest / Key Item",
};

struct SocketGem {
    uint32_t itemKey = 0;
    uint16_t endurance = 65535;
};

struct ItemSpec {
    uint32_t itemKey = 0;
    uint64_t itemNo = 0;
    uint16_t slotNo = 0;
    uint64_t stackCount = 1;
    uint16_t enchantLevel = 0;
    uint16_t endurance = 65535;
    uint16_t sharpness = 0;
    uint8_t maxSocketCount = 0;
    std::vector<SocketGem> sockets;
    bool isNewMark = false;
    bool isLocked = false;
    ItemMask maskType = ItemMask::EquipmentNoSocket;
};

std::vector<uint8_t> BuildItem(
    const ItemSpec& spec,
    const std::unordered_map<std::string, uint16_t>& type_index_map);

std::vector<uint8_t> BuildMaskBytes(ItemMask mask, const ItemSpec& spec);

} // namespace ItemFactory
