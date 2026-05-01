# PABGB Files Touched by CrimsonGameMods

All `.pabgb` / `.pabgh` files that CrimsonGameMods reads from or writes to the game.
Vanilla source is always PAZ group `0008`. All overlay groups above `0035` are configurable via spin boxes in each tab — values listed are defaults.

---

## Files Written to Game (Overlays)

| # | File | Default Overlay | Tab | Notes |
|---|------|-----------------|-----|-------|
| 1 | `iteminfo.pabgb` / `.pabgh` | `0058` | ItemBuffs | Stat buffs, transmog, sockets, passives, dye, abyss unlock |
| 2 | `iteminfo.pabgb` / `.pabgh` | `0062` | Stacker | Multi-mod merge (ItemBuffs + DMM + external mods) |
| 3 | `iteminfo.pabgb` / `.pabgh` | `0036` | Item Creator | Shareable custom items + dropsetinfo |
| 4 | `equipslotinfo.pabgb` / `.pabgh` | `0059` | ItemBuffs | Universal Proficiency v2 (tribe_gender removal) |
| 5 | `equipslotinfo.pabgb` / `.pabgh` | `0063` | Stacker / ReserveSlot | Merged equipslot + reserve slot edits |
| 6 | `characterinfo.pabgb` / `.pabgh` | `0058` (bundled) | ItemBuffs / FieldEdit | Mesh swap (appearance key), mount cooldown/killable/invincible |
| 7 | `characterinfo.pabgb` / `.pabgh` | `0062` | FieldEdit | Mount overlay (rider bone + scale) |
| 8 | `mercenaryinfo.pabgb` / `.pabgh` | `0065` | MercPets | Pet/mercenary/vehicle caps and flags |
| 9 | `storeinfo.pabgb` / `.pabgh` | `0060` | Stores | Vendor item lists, prices, stock limits |
| 10 | `dropsetinfo.pabgb` / `.pabgh` | `0036` | DropSets / Item Creator | Drop tables, rates, quantities |
| 11 | `skilltreeinfo.pabgb` / `.pabgh` | `0064` | SkillTree | Skill tree structure, weapon trees, character gates |
| 12 | `skilltreegroupinfo.pabgb` / `.pabgh` | `0064` | SkillTree | Skill tree group definitions |
| 13 | `skill.pabgb` / `.pabgh` | `0058` (bundled) | ItemBuffs / SkillTree | Passive skill data, imbue, buff references |
| 14 | `inventory.pabgb` / `.pabgh` | `0061` | BagSpace | Inventory slot counts |
| 15 | `reserveslot.pabgb` / `.pabgh` | `0066` | ReserveSlot | Reserve slot configuration |
| 16 | `fieldinfo.pabgb` / `.pabgh` | `0062` | FieldEdit | Field/region data edits |
| 17 | `vehicleinfo.pabgb` / `.pabgh` | `0062` | FieldEdit | Vehicle data edits |
| 18 | `gameplaytrigger.pabgb` / `.pabgh` | `0062` | FieldEdit | Gameplay trigger edits |
| 19 | `regioninfo.pabgb` / `.pabgh` | `0062` | FieldEdit | Region data edits |
| 20 | `wantedinfo.pabgb` / `.pabgh` | `0062` | FieldEdit | Wanted system edits |
| 21 | `allygroupinfo.pabgb` / `.pabgh` | `0062` | FieldEdit | Ally group edits |
| 22 | `factionrelationgroup.pabgb` / `.pabgh` | `0062` | FieldEdit | Faction relation edits |
| 23 | `factionnode.pabgb` / `.pabgh` | `0037` | SpawnEdit | Faction node data |
| 24 | `factionnodespawninfo.pabgb` | `0037` | SpawnEdit | Faction node spawn rules |
| 25 | `terrainregionautospawninfo.pabgb` / `.pabgh` | `0037` | SpawnEdit | Terrain region auto-spawn config |
| 26 | `spawningpoolautospawninfo.pabgb` / `.pabgh` | `0037` | SpawnEdit | Spawning pool auto-spawn config |
| 27 | `conditioninfo.pabgb` / `.pabgh` | `0062` | Character Unlock | Mission gate conditions |
| 28 | `localizationstring_eng.paloc` | `0064` | ItemBuffs (Item Creator) | Custom item names |

---

## Files Read Only (Not Deployed)

| File | Used By | Purpose |
|------|---------|---------|
| `stringinfo.pabgb` | FieldEdit, StringResolver | 29K hash-to-string lookup for display names |
| `knowledgeinfo.pabgb` | Knowledge pack system | Knowledge entries for abyss gate unlock |
| `partprefabdyeslotinfo.pabgb` | Dye system | 1076 item dye slot definitions |
| `gimmickinfo.pabgb` / `.pabgh` | ItemBuffs | Effect/imbue catalog for gimmick browsing |
| `buffinfo.pabgb` | Buff name chain | Buff definitions, name resolution |
| `questinfo.pabgb` | Quest system | Quest chain definitions, sub-quest key lists |

---

## Overlay Group Map

| Group | Default Owner | Contents |
|-------|---------------|----------|
| `0036` | Item Creator | iteminfo + dropsetinfo (shareable) |
| `0037` | SpawnEdit | terrainregionautospawninfo + spawningpoolautospawninfo + factionnode + factionnodespawninfo |
| `0058` | ItemBuffs | iteminfo + characterinfo (staged) + skill (staged) |
| `0059` | ItemBuffs | equipslotinfo (UP v2) |
| `0060` | Stores | storeinfo |
| `0061` | BagSpace | inventory |
| `0062` | FieldEdit / Stacker | fieldinfo + vehicleinfo + gameplaytrigger + regioninfo + characterinfo + wantedinfo + allygroupinfo + factionrelationgroup + iteminfo (Stacker merged) |
| `0063` | Stacker | equipslotinfo (merged) |
| `0064` | SkillTree / ItemBuffs | skilltreeinfo + skilltreegroupinfo (or localizationstring_eng.paloc) |
| `0065` | MercPets | mercenaryinfo |
| `0066` | ReserveSlot | reserveslot |
