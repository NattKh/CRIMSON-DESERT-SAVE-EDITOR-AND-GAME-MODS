#include "item_factory.h"
#include <cstring>

namespace ItemFactory {

static void PushU8(std::vector<uint8_t>& buf, uint8_t v) { buf.push_back(v); }
static void PushU16(std::vector<uint8_t>& buf, uint16_t v) {
    buf.push_back((uint8_t)(v & 0xFF));
    buf.push_back((uint8_t)((v >> 8) & 0xFF));
}
static void PushU32(std::vector<uint8_t>& buf, uint32_t v) {
    buf.push_back((uint8_t)(v & 0xFF));
    buf.push_back((uint8_t)((v >> 8) & 0xFF));
    buf.push_back((uint8_t)((v >> 16) & 0xFF));
    buf.push_back((uint8_t)((v >> 24) & 0xFF));
}
static void PushU64(std::vector<uint8_t>& buf, uint64_t v) {
    for (int i = 0; i < 8; i++) buf.push_back((uint8_t)((v >> (i * 8)) & 0xFF));
}
static void PatchU32(std::vector<uint8_t>& buf, uint32_t offset, uint32_t v) {
    memcpy(buf.data() + offset, &v, 4);
}

std::vector<uint8_t> BuildMaskBytes(ItemMask mask, const ItemSpec& spec) {
    // 25 fields → need 4 mask bytes (32 bits, only 25 used)
    uint32_t bits = 0;

    // Base masks extracted from real game items (6 patterns from endgame save).
    // The most common equipment pattern (8/18 items) is 0x002D289F:
    //   bits 0,1,2,3,4,7,11,13,16,18,19,21
    //   = saveVersion, itemNo, itemKey, slotNo, stackCount, endurance,
    //     maxSocketCount, socketSaveDataList, transferredItemKey,
    //     maxChargeUseableCount, chargedUseableCount, timeWhenPushItem

    switch (mask) {
    case ItemMask::SimpleConsumable:
        // Consumable/material: same base but no sockets/endurance
        bits = (1u<<0)|(1u<<1)|(1u<<2)|(1u<<3)|(1u<<4)|(1u<<7)|
               (1u<<16)|(1u<<18)|(1u<<19)|(1u<<21);
        if (spec.isNewMark) bits |= (1u<<23);
        break;

    case ItemMask::Equipment:
        // Full equipment: real game mask 0x002D289F + optional fields
        bits = (1u<<0)|(1u<<1)|(1u<<2)|(1u<<3)|(1u<<4)|(1u<<7)|
               (1u<<11)|(1u<<13)|(1u<<16)|(1u<<18)|(1u<<19)|(1u<<21);
        if (spec.enchantLevel > 0) bits |= (1u<<5);
        if (spec.sharpness > 0) bits |= (1u<<8);
        if (!spec.sockets.empty()) bits |= (1u<<12); // validSocketCount
        if (spec.isNewMark) bits |= (1u<<23);
        if (spec.isLocked) bits |= (1u<<24);
        break;

    case ItemMask::EquipmentNoSocket:
        // Equipment without socket list
        bits = (1u<<0)|(1u<<1)|(1u<<2)|(1u<<3)|(1u<<4)|(1u<<7)|
               (1u<<16)|(1u<<18)|(1u<<19)|(1u<<21);
        if (spec.enchantLevel > 0) bits |= (1u<<5);
        if (spec.endurance < 65535) bits |= (1u<<7);
        if (spec.sharpness > 0) bits |= (1u<<8);
        if (spec.isNewMark) bits |= (1u<<23);
        break;

    case ItemMask::QuestItem:
        // Quest item: minimal but with endurance + transfer key
        bits = (1u<<0)|(1u<<1)|(1u<<2)|(1u<<3)|(1u<<4)|(1u<<7)|
               (1u<<16)|(1u<<18)|(1u<<19)|(1u<<21);
        if (spec.isNewMark) bits |= (1u<<23);
        break;
    }

    std::vector<uint8_t> mask_bytes(4);
    mask_bytes[0] = (uint8_t)(bits & 0xFF);
    mask_bytes[1] = (uint8_t)((bits >> 8) & 0xFF);
    mask_bytes[2] = (uint8_t)((bits >> 16) & 0xFF);
    mask_bytes[3] = (uint8_t)((bits >> 24) & 0xFF);
    return mask_bytes;
}

static std::vector<uint8_t> BuildSocketList(
    const std::vector<SocketGem>& sockets,
    uint16_t socket_type_index) {
    // List header: prefix=0, count=LE_u24, reserved1-3(u32×3), reserved4(u16) = 18 bytes
    std::vector<uint8_t> list;
    uint32_t count = (uint32_t)sockets.size();
    PushU8(list, 0); // prefix
    PushU8(list, (uint8_t)(count & 0xFF));
    PushU8(list, (uint8_t)((count >> 8) & 0xFF));
    PushU8(list, (uint8_t)((count >> 16) & 0xFF));
    PushU32(list, 0); // reserved1
    PushU32(list, 0); // reserved2
    PushU32(list, 0); // reserved3
    PushU16(list, 0); // reserved4

    // Each socket element: compact format (MBC=1)
    for (auto& gem : sockets) {
        uint32_t elem_start = (uint32_t)list.size();
        // Header: MBC=1, mask=0x03 (2 fields present: _currentEndurance + _itemKey)
        PushU16(list, 1); // MBC
        PushU8(list, 0x03); // mask: bits 0,1 set
        PushU16(list, socket_type_index);
        PushU8(list, 0); // reserved
        // Sentinel
        for (int i = 0; i < 8; i++) PushU8(list, 0xFF);
        // PO placeholder (will be fixed by serializer)
        uint32_t po_pos = (uint32_t)list.size();
        PushU32(list, 0);
        uint32_t payload_start = (uint32_t)list.size();
        PatchU32(list, po_pos, payload_start); // self-referential (will be corrected later)
        // Payload
        PushU32(list, 0); // reserved_u32
        PushU16(list, gem.endurance); // _currentEndurance
        PushU32(list, gem.itemKey); // _itemKey
        // Trailing size
        uint32_t trailing_size = (uint32_t)list.size() - payload_start;
        PushU32(list, trailing_size);
    }
    return list;
}

std::vector<uint8_t> BuildItem(
    const ItemSpec& spec,
    const std::unordered_map<std::string, uint16_t>& type_index_map) {

    // For equipment masks: ensure at least 5 empty sockets if none specified
    ItemSpec s = spec;
    if ((s.maskType == ItemMask::Equipment) && s.sockets.empty()) {
        s.maxSocketCount = 5;
        for (int i = 0; i < 5; i++)
            s.sockets.push_back({0, 65535}); // empty socket: key=0, full endurance
    }

    auto item_ti_it = type_index_map.find("ItemSaveData");
    if (item_ti_it == type_index_map.end()) return {};
    uint16_t item_type_index = item_ti_it->second;

    uint16_t socket_type_index = 0;
    auto socket_ti_it = type_index_map.find("ItemSocketSaveData");
    if (socket_ti_it != type_index_map.end()) socket_type_index = socket_ti_it->second;

    ItemMask mask_type = s.maskType;
    auto mask_bytes = BuildMaskBytes(mask_type, s);
    // Keep all 4 mask bytes (game format — do NOT trim trailing zeros)
    uint16_t mbc = (uint16_t)mask_bytes.size();

    std::vector<uint8_t> buf;
    buf.reserve(300);

    // Element header
    PushU16(buf, mbc);
    for (auto b : mask_bytes) PushU8(buf, b);
    PushU16(buf, item_type_index);
    PushU8(buf, 0); // reserved
    for (int i = 0; i < 8; i++) PushU8(buf, 0xFF); // sentinel
    uint32_t po_pos = (uint32_t)buf.size();
    PushU32(buf, 0); // PO placeholder
    uint32_t payload_start = (uint32_t)buf.size();
    PatchU32(buf, po_pos, payload_start);

    // Payload
    PushU32(buf, 0); // reserved_u32

    // Compute which bits are set
    uint32_t bits = 0;
    for (size_t i = 0; i < mask_bytes.size(); i++)
        bits |= ((uint32_t)mask_bytes[i]) << (i * 8);

    // Write fields in order (only if bit is set)
    if (bits & (1u<<0))  PushU32(buf, 1);                    // _saveVersion
    if (bits & (1u<<1))  PushU64(buf, s.itemNo);              // _itemNo
    if (bits & (1u<<2))  PushU32(buf, s.itemKey);             // _itemKey
    if (bits & (1u<<3))  PushU16(buf, s.slotNo);              // _slotNo
    if (bits & (1u<<4))  PushU64(buf, s.stackCount);          // _stackCount
    if (bits & (1u<<5))  PushU16(buf, s.enchantLevel);        // _enchantLevel
    if (bits & (1u<<6))  PushU64(buf, 0);                     // _useableCtc
    if (bits & (1u<<7))  PushU16(buf, s.endurance);            // _endurance
    if (bits & (1u<<8))  PushU16(buf, s.sharpness);            // _sharpness
    // bits 9,10: _batteryStat, _maxBatteryStat (not implemented)
    if (bits & (1u<<11)) PushU8(buf, s.maxSocketCount);        // _maxSocketCount
    if (bits & (1u<<12)) PushU8(buf, (uint8_t)s.sockets.size()); // _validSocketCount
    if (bits & (1u<<13)) {
        auto socket_bytes = BuildSocketList(s.sockets, socket_type_index);
        buf.insert(buf.end(), socket_bytes.begin(), socket_bytes.end());
    }
    // bits 14,15: _itemDyeDataList, _dropResultSubSaveItemList (not implemented)
    if (bits & (1u<<16)) PushU32(buf, ((s.itemKey & 0xFFFF) << 16) | 0x0101); // _transferredItemKey
    // bit 17: _currentGimmickState
    if (bits & (1u<<18)) PushU32(buf, 0x00010001);             // _maxChargeUseableCount (game default)
    if (bits & (1u<<19)) PushU64(buf, 0);                    // _chargedUseableCount
    if (bits & (1u<<20)) PushU64(buf, 0);                    // _coolTimePerCharge
    if (bits & (1u<<21)) PushU64(buf, 0);                    // _timeWhenPushItem
    // bit 22: _characterConversionData
    if (bits & (1u<<23)) PushU8(buf, s.isNewMark ? 1 : 0);     // _isNewMark
    if (bits & (1u<<24)) PushU8(buf, s.isLocked ? 1 : 0);      // _isLocked

    // Trailing size
    uint32_t trailing_size = (uint32_t)buf.size() - payload_start;
    PushU32(buf, trailing_size);

    return buf;
}

} // namespace ItemFactory
