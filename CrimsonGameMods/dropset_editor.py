
import struct
import json
import os
import copy
from data_db import get_connection
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class ItemDrop:
    flag: int
    item_key: int
    unk3: int = 0
    unk4: int = 0
    unk1_flag: bytes = b'\x00' * 5
    unk_cond_flag: int = 0
    unk_post_cond: int = 0
    rates: int = 0
    rates_100: int = 0
    unk2: int = 0
    max_amt: int = 0
    min_amt: int = 0
    unk3_flags: int = 0xFFFF
    item_key_dup: int = 0
    extra_u8: Optional[int] = None
    extra_u32: Optional[int] = None
    friendly_data: Optional[bytes] = None
    body_offset: int = 0
    size: int = 0


@dataclass
class DropSet:
    key: int
    name: str
    is_blocked: int
    drop_roll_type: int = 0
    drop_roll_count: int = 0
    drop_condition_string: str = ""
    drop_tag_name_hash: int = 0
    drops: List[ItemDrop] = field(default_factory=list)
    nee_slot_count: int = -1
    need_weight: int = 0
    total_drop_rate: int = 0
    original_string: str = ""
    header_offset: int = 0
    body_offset: int = 0
    total_size: int = 0


class DropsetEditor:

    CHEST_KEYS = {
        170204: "DropSet_Chest_Tier1",
        170205: "DropSet_Chest_Tier2",
        170206: "DropSet_Chest_Tier3",
        170207: "DropSet_Chest_Tier4",
    }

    CHEST_ITEM_KEYS = {
        100101: "DropSet_ChestItem_0002",
        100102: "DropSet_ChestItem_0003",
        100103: "DropSet_ChestItem_004",
        100104: "DropSet_ChestItem_009",
        100130: "DropSet_ChestItem_010",
        100105: "DropSet_Barrel_ChestItem_0001",
        100106: "DropSet_ChestItem_0005",
        100107: "DropSet_ChestItem_0006",
        100108: "DropSet_ChestItem_0007",
        100109: "DropSet_ChestItem_0008",
    }

    MAX_RATE = 1_000_000

    def __init__(self):
        self.header_bytes: bytes = b""
        self.body_bytes: bytearray = bytearray()
        self.record_count: int = 0
        self.records: List[Tuple[int, int]] = []
        self.item_names: Dict[int, str] = {}
        self._parsed_sets: Dict[int, DropSet] = {}

    def load(self, pabgh_path: str, pabgb_path: str):
        self.header_bytes = open(pabgh_path, "rb").read()
        self.body_bytes = bytearray(open(pabgb_path, "rb").read())

        self.record_count = struct.unpack_from("<H", self.header_bytes, 0)[0]
        self.records = []
        for i in range(self.record_count):
            off = 2 + i * 8
            key, offset = struct.unpack_from("<II", self.header_bytes, off)
            self.records.append((key, offset))

    def load_item_names(self, item_names_path: str = ""):
        _db = get_connection()
        for row in _db.execute("SELECT item_key, name FROM items"):
            self.item_names[row['item_key']] = row['name']

    def get_item_name(self, key: int) -> str:
        return self.item_names.get(key, f"Item_{key}")

    def _parse_drop_entry(self, pos: int) -> Tuple[ItemDrop, int]:
        start = pos
        flag = self.body_bytes[pos]; pos += 1
        item_key = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        unk3 = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        unk4 = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        unk1_flag = bytes(self.body_bytes[pos:pos+5]); pos += 5
        unk_cond_flag = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        unk_post_cond = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        rates = struct.unpack_from("<Q", self.body_bytes, pos)[0]; pos += 8
        rates_100 = struct.unpack_from("<Q", self.body_bytes, pos)[0]; pos += 8
        unk2 = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        max_amt = struct.unpack_from("<Q", self.body_bytes, pos)[0]; pos += 8
        min_amt = struct.unpack_from("<Q", self.body_bytes, pos)[0]; pos += 8
        unk3_flags = struct.unpack_from("<H", self.body_bytes, pos)[0]; pos += 2
        item_key_dup = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        extra_u8 = None
        extra_u32 = None
        friendly_data = None
        if unk4 == 13:
            extra_u8 = self.body_bytes[pos]; pos += 1
        elif unk4 == 10:
            extra_u32 = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        elif unk4 == 7:
            friendly_data = bytes(self.body_bytes[pos:pos+28]); pos += 28

        drop = ItemDrop(
            flag=flag, item_key=item_key,
            unk3=unk3, unk4=unk4, unk1_flag=unk1_flag, unk_cond_flag=unk_cond_flag,
            unk_post_cond=unk_post_cond,
            rates=rates, rates_100=rates_100, unk2=unk2,
            max_amt=max_amt, min_amt=min_amt, unk3_flags=unk3_flags,
            item_key_dup=item_key_dup,
            extra_u8=extra_u8, extra_u32=extra_u32, friendly_data=friendly_data,
            body_offset=start, size=pos - start,
        )
        return drop, pos

    def _serialize_drop_entry(self, drop: ItemDrop) -> bytes:
        buf = bytearray()
        buf.append(drop.flag)
        buf += struct.pack("<I", drop.item_key)
        buf += struct.pack("<I", drop.unk3)
        buf += struct.pack("<I", drop.unk4)
        buf += drop.unk1_flag
        buf += struct.pack("<I", drop.unk_cond_flag)
        buf += struct.pack("<I", drop.unk_post_cond)
        buf += struct.pack("<Q", drop.rates)
        buf += struct.pack("<Q", drop.rates_100)
        buf += struct.pack("<I", drop.unk2)
        buf += struct.pack("<Q", max(0, drop.max_amt))
        buf += struct.pack("<Q", max(0, drop.min_amt))
        buf += struct.pack("<H", drop.unk3_flags)
        buf += struct.pack("<I", drop.item_key_dup)
        if drop.unk4 == 13 and drop.extra_u8 is not None:
            buf.append(drop.extra_u8)
        elif drop.unk4 == 10 and drop.extra_u32 is not None:
            buf += struct.pack("<I", drop.extra_u32)
        elif drop.unk4 == 7 and drop.friendly_data is not None:
            buf += drop.friendly_data
        return bytes(buf)

    def parse_dropset(self, key: int) -> Optional[DropSet]:
        if key in self._parsed_sets:
            return self._parsed_sets[key]

        offset = None
        for k, o in self.records:
            if k == key:
                offset = o
                break
        if offset is None:
            return None

        pos = offset
        rec_key = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        name_len = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        name = self.body_bytes[pos:pos+name_len].decode("ascii", errors="replace"); pos += name_len
        is_blocked = self.body_bytes[pos]; pos += 1
        drop_roll_type = self.body_bytes[pos]; pos += 1
        drop_roll_count = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        dcs_len = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        drop_condition_string = ""
        if dcs_len > 0:
            drop_condition_string = self.body_bytes[pos:pos+dcs_len].decode("ascii", errors="replace")
            pos += dcs_len
        drop_tag_name_hash = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        drop_count = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4

        drops = []
        for _ in range(drop_count):
            drop, pos = self._parse_drop_entry(pos)
            drops.append(drop)

        nee_slot_count = struct.unpack_from("<h", self.body_bytes, pos)[0]; pos += 2
        need_weight = struct.unpack_from("<q", self.body_bytes, pos)[0]; pos += 8
        total_drop_rate = struct.unpack_from("<q", self.body_bytes, pos)[0]; pos += 8
        code_len = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
        original_string = self.body_bytes[pos:pos+code_len].decode("latin-1", errors="replace"); pos += code_len

        ds = DropSet(
            key=rec_key, name=name, is_blocked=is_blocked,
            drop_roll_type=drop_roll_type, drop_roll_count=drop_roll_count,
            drop_condition_string=drop_condition_string,
            drop_tag_name_hash=drop_tag_name_hash, drops=drops,
            nee_slot_count=nee_slot_count, need_weight=need_weight,
            total_drop_rate=total_drop_rate,
            original_string=original_string,
            header_offset=offset, body_offset=offset,
            total_size=pos - offset,
        )
        self._parsed_sets[key] = ds
        return ds

    def _serialize_dropset(self, ds: DropSet) -> bytes:
        buf = bytearray()
        buf += struct.pack("<I", ds.key)
        name_bytes = ds.name.encode("latin-1", errors="replace")
        buf += struct.pack("<I", len(name_bytes))
        buf += name_bytes
        buf.append(ds.is_blocked)
        buf.append(ds.drop_roll_type)
        buf += struct.pack("<I", ds.drop_roll_count)
        dcs_bytes = ds.drop_condition_string.encode("latin-1", errors="replace") if ds.drop_condition_string else b""
        buf += struct.pack("<I", len(dcs_bytes))
        buf += dcs_bytes
        buf += struct.pack("<I", ds.drop_tag_name_hash)
        buf += struct.pack("<I", len(ds.drops))
        for drop in ds.drops:
            buf += self._serialize_drop_entry(drop)
        buf += struct.pack("<h", ds.nee_slot_count)
        buf += struct.pack("<q", ds.need_weight)
        buf += struct.pack("<q", ds.total_drop_rate)
        orig_bytes = ds.original_string.encode("latin-1", errors="replace")
        buf += struct.pack("<I", len(orig_bytes))
        buf += orig_bytes
        return bytes(buf)

    def get_chest_tiers(self) -> Dict[str, DropSet]:
        result = {}
        for key, label in self.CHEST_KEYS.items():
            ds = self.parse_dropset(key)
            if ds:
                result[label] = ds
        return result


    def boost_rates(self, ds: DropSet, rate: int = MAX_RATE):
        for drop in ds.drops:
            drop.rates = rate
            drop.rates_100 = rate // 10000

    def boost_quantities(self, ds: DropSet, min_qty: int = 10, max_qty: int = 20):
        for drop in ds.drops:
            drop.max_amt = max(0, min_qty)
            drop.min_amt = max(0, max_qty)

    def swap_item(self, ds: DropSet, old_key: int, new_key: int):
        for drop in ds.drops:
            if drop.item_key == old_key:
                drop.item_key = new_key
                if drop.item_key_dup == old_key:
                    drop.item_key_dup = new_key
                return True
        return False

    def add_item(self, ds: DropSet, item_key: int, rate: int = MAX_RATE,
                 min_qty: int = 1, max_qty: int = 1,
                 template_drop: Optional[ItemDrop] = None) -> ItemDrop:
        if template_drop is None:
            template_drop = ds.drops[0] if ds.drops else None

        new_drop = ItemDrop(
            flag=template_drop.flag if template_drop else 1,
            item_key=item_key,
            unk3=template_drop.unk3 if template_drop else 0,
            unk4=0,
            unk1_flag=template_drop.unk1_flag if template_drop else b'\x00' * 5,
            unk_cond_flag=template_drop.unk_cond_flag if template_drop else 0xFFFFFFFF,
            unk_post_cond=template_drop.unk_post_cond if template_drop else 0,
            rates=rate,
            rates_100=rate // 10000,
            unk2=0,
            max_amt=min_qty,
            min_amt=max_qty,
            unk3_flags=0xFFFF,
            item_key_dup=item_key,
            body_offset=0,
            size=68,
        )
        ds.drops.append(new_drop)
        return new_drop

    def remove_item(self, ds: DropSet, item_key: int) -> bool:
        for i, drop in enumerate(ds.drops):
            if drop.item_key == item_key:
                ds.drops.pop(i)
                return True
        return False

    def apply_modifications(self, modified_sets: List[DropSet]):
        mod_info = []
        for ds in modified_sets:
            new_data = self._serialize_dropset(ds)
            old_size = ds.total_size
            delta = len(new_data) - old_size
            mod_info.append((ds.body_offset, old_size, new_data, delta))
            ds.total_size = len(new_data)

        mod_info.sort(key=lambda x: x[0], reverse=True)

        for orig_offset, old_size, new_data, delta in mod_info:
            self.body_bytes[orig_offset:orig_offset + old_size] = new_data

        mod_info.sort(key=lambda x: x[0])
        new_header = bytearray(self.header_bytes)

        for i in range(self.record_count):
            hdr_off = 2 + i * 8 + 4
            rec_offset = struct.unpack_from("<I", new_header, hdr_off)[0]
            shift = 0
            for orig_offset, _, _, delta in mod_info:
                if orig_offset < rec_offset:
                    shift += delta
                else:
                    break
            if shift != 0:
                struct.pack_into("<I", new_header, hdr_off, rec_offset + shift)

        self.header_bytes = bytes(new_header)

        self.records = []
        for i in range(self.record_count):
            off = 2 + i * 8
            key, offset = struct.unpack_from("<II", self.header_bytes, off)
            self.records.append((key, offset))
        self._parsed_sets.clear()

    def save(self, pabgh_path: str, pabgb_path: str):
        with open(pabgh_path, "wb") as f:
            f.write(self.header_bytes)
        with open(pabgb_path, "wb") as f:
            f.write(self.body_bytes)


    @staticmethod
    def categorize_by_name(name: str) -> str:
        if not name:
            return "Unnamed"
        low = name.lower()
        if "chest_tier" in low:
            return "Chest Tier"
        if "chestitem" in low or "chest" in low:
            return "Chest"
        if "barrel" in low or "boxbarrel" in low:
            return "Container"
        if "faction" in low:
            return "Faction"
        if "abyssgear" in low:
            return "Abyss Gear"
        if "character" in low:
            return "Character"
        if "contribution" in low:
            return "Contribution"
        if "camp" in low:
            return "Camp"
        if "fish" in low:
            return "Fishing"
        if "dungeon" in low:
            return "Dungeon"
        if "farm" in low or "seed" in low:
            return "Farming"
        if "money" in low or "copper" in low:
            return "Money"
        if "legendary" in low:
            return "Legendary"
        if "knowledge" in low:
            return "Knowledge"
        if "mercenary" in low:
            return "Mercenary"
        if "operation" in low:
            return "Operation"
        if "minigame" in low or "seotda" in low:
            return "Mini-Game"
        if "gimmick" in low:
            return "Gimmick"
        if "restarea" in low:
            return "Rest Area"
        if "arrow" in low:
            return "Arrow"
        if "reward" in low:
            return "Reward"
        if "animal" in low:
            return "Animal"
        if "random" in low:
            return "Random"
        return "Other"

    def get_all_sets_summary(self, named_only: bool = True) -> List[dict]:
        result = []
        sorted_recs = sorted(self.records, key=lambda r: r[1])
        offset_to_next = {}
        for i, (k, o) in enumerate(sorted_recs):
            next_o = sorted_recs[i+1][1] if i+1 < len(sorted_recs) else len(self.body_bytes)
            offset_to_next[o] = next_o

        for key, offset in self.records:
            pos = offset
            rec_key = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
            name_len = struct.unpack_from("<I", self.body_bytes, pos)[0]; pos += 4
            if name_len > 500:
                continue
            name = ""
            if name_len > 0:
                name = self.body_bytes[pos:pos+name_len].decode("ascii", errors="replace")
            pos += name_len
            pos += 1 + 1 + 4 + 4 + 4
            drop_count = struct.unpack_from("<I", self.body_bytes, pos)[0]

            if named_only and not name:
                continue

            category = self.categorize_by_name(name)
            result.append({
                "key": key,
                "name": name,
                "drop_count": drop_count,
                "category": category,
                "offset": offset,
            })

        result.sort(key=lambda r: (0 if r["name"] else 1, r["name"] or "", r["key"]))
        return result

    def get_categories(self, named_only: bool = True) -> List[str]:
        summaries = self.get_all_sets_summary(named_only=named_only)
        cats = sorted(set(s["category"] for s in summaries))
        return cats

    def print_dropset(self, ds: DropSet, show_rates: bool = True):
        safe_name = ds.name.encode("ascii", errors="replace").decode("ascii")
        print(f"\n{'='*60}")
        print(f"  {safe_name}  (key={ds.key}, {len(ds.drops)} items)")
        print(f"{'='*60}")
        for i, d in enumerate(ds.drops):
            name = self.get_item_name(d.item_key)
            rate_pct = d.rates / 10000
            qty = f"{d.max_amt}-{d.min_amt}" if d.max_amt != d.min_amt else str(d.max_amt)
            print(f"  [{i:>2}] {d.item_key:>8}  {rate_pct:>6.1f}%  x{qty:<6}  {name}")


ENCHANT_SCROLLS = {
    1002896: "Steelbane I",
    1002899: "Hexebane I",
    1002902: "Bloodbane I",
    1002905: "Primalbane I",
    1002908: "Malicebane I",
    1002911: "Beastbane I",
    1002914: "Abyssbane I",
    1003759: "Shatter I",
    1003762: "Rend I",
    1003765: "Shred I",
    1002953: "Energy Drain I",
    1003229: "Disarm I",
    1002920: "Fortune I",
    1002923: "Infinite Arrows I",
}

VALUABLE_GEMS = {
    721504: "Diamond",
    721506: "Scolecite Ore",
    720003: "Gold Ore",
    720010: "Bismuth Ore",
}

RARE_MATERIALS = {
    1001321: "Explosive Arrow",
    1003735: "Small Battery",
    1003756: "Lubricant",
    1002971: "Cogwheel",
    30010: "Gunpowder",
}

SILVER_POUCHES = {
    103: "Light Copper Pouch",
    102: "Full Copper Pouch",
    1000629: "Modest Silver Pouch",
    101: "Silver Pouch",
}

TRADE_GOODS = [
    1000619,
    1000661,
    1000667,
    1000668,
    1000598,
    1000643,
    1000602,
    1000603,
    1000646,
    1000648,
    1000608,
    1000612,
]


def apply_loot_bonanza(editor: DropsetEditor):
    tiers = editor.get_chest_tiers()

    for label, ds in tiers.items():
        print(f"\nModifying {label}...")

        editor.boost_rates(ds, rate=1_000_000)

        for drop in ds.drops:
            if drop.item_key in TRADE_GOODS:
                drop.max_amt = max(drop.max_amt, 5)
                drop.min_amt = max(drop.min_amt, 10)
            else:
                drop.max_amt = max(drop.max_amt * 3, 3)
                drop.min_amt = max(drop.min_amt * 3, 5)

        scroll_keys = list(ENCHANT_SCROLLS.keys())
        for i, trade_key in enumerate(TRADE_GOODS):
            if i < len(scroll_keys):
                found = False
                for drop in ds.drops:
                    if drop.item_key == trade_key:
                        new_key = scroll_keys[i]
                        drop.item_key = new_key
                        drop.item_key_dup = new_key
                        drop.rates = 500_000
                        drop.rates_100 = 50
                        drop.max_amt = 1
                        drop.min_amt = 2
                        found = True
                        break

        existing_keys = {d.item_key for d in ds.drops}
        for gem_key in [721504, 720003]:
            if gem_key not in existing_keys:
                editor.add_item(ds, gem_key, rate=800_000, min_qty=2, max_qty=5)

    return list(tiers.values())


def apply_generous(editor: DropsetEditor):
    tiers = editor.get_chest_tiers()

    for label, ds in tiers.items():
        editor.boost_rates(ds, rate=1_000_000)
        for drop in ds.drops:
            drop.max_amt = max(drop.max_amt * 3, 3)
            drop.min_amt = max(drop.min_amt * 3, 5)

    return list(tiers.values())


def main():
    import argparse

    base = os.path.dirname(os.path.abspath(__file__))
    extracted = os.path.join(base, "..", "extractedpaz", "0008_full")

    parser = argparse.ArgumentParser(description="Dropset Editor for Crimson Desert")
    parser.add_argument("--preset", choices=["bonanza", "generous", "show"],
                        default="show", help="Preset to apply")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory for modified pabgb/pabgh")
    args = parser.parse_args()

    editor = DropsetEditor()
    editor.load(
        os.path.join(extracted, "dropsetinfo.pabgh"),
        os.path.join(extracted, "dropsetinfo.pabgb"),
    )

    editor.load_item_names()

    if args.preset == "show":
        tiers = editor.get_chest_tiers()
        for label, ds in tiers.items():
            editor.print_dropset(ds)
        return

    elif args.preset == "bonanza":
        modified = apply_loot_bonanza(editor)
    elif args.preset == "generous":
        modified = apply_generous(editor)

    editor.apply_modifications(modified)

    tiers = editor.get_chest_tiers()
    for label, ds in tiers.items():
        editor.print_dropset(ds)

    if args.output:
        os.makedirs(args.output, exist_ok=True)
        editor.save(
            os.path.join(args.output, "dropsetinfo.pabgh"),
            os.path.join(args.output, "dropsetinfo.pabgb"),
        )
        print(f"\nSaved to {args.output}/")
    else:
        print("\nDry run — use --output <dir> to save modified files.")


if __name__ == "__main__":
    main()
