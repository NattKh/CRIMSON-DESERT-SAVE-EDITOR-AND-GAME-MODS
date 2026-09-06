import struct
import json
import os
import logging

log = logging.getLogger(__name__)

QUEST_STATES = {
    0: 'Unknown',
    1: 'Locked',
    2: 'Available',
    3: 'InProgress',
    4: 'CompletionReady',
    5: 'Completed',
    6: 'RewardReceived',
}


class StageEntry:
    __slots__ = ('key', 'state', 'complete_type', 'is_wait_branch', 'is_skip_complete',
                 'completed_count', 'completed_time', 'delayed_time', 'branched_time',
                 'position', 'delayed_from_mission', 'delayed_from_stage',
                 'sub_timeline_name', 'connected_actors',
                 'state_offset', 'element_start', 'element_end')

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)
        self.key = 0
        self.state = 0
        self.connected_actors = []


class QuestEntry:
    __slots__ = ('key', 'state', 'state_offset', 'state_size',
                 'branched_time', 'completed_time', 'delayed_from_quest',
                 'delay_time', 'name', 'mask_hex',
                 'element_start', 'element_end',
                 'stage_keys', 'mission_keys', 'stages', 'missions')

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)
        self.key = 0
        self.state = 0
        self.stage_keys = []
        self.mission_keys = []
        self.stages = []
        self.missions = []


class MissionEntry:
    __slots__ = ('key', 'state', 'state_offset', 'state_size',
                 'branched_time', 'completed_time', 'delayed_time',
                 'complete_count', 'name', 'mask_hex',
                 'element_start', 'element_end')

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)
        self.key = 0
        self.state = 0


class GimmickLink:
    __slots__ = (
        'gimmick_key', 'gimmick_info_key', 'stage_key',
        'save_root_key', 'owner_level_name',
        'is_broken', 'is_lock_state', 'save_by_cheat',
        'is_spread_broken', 'is_logout_from_gimmick',
        'is_logout_away', 'is_activate_away',
        'is_raise_complete',
        'field_save_reason', 'spawn_reason', 'spawn_style',
        'reset_time', 'left_drop_roll_count',
        'installation_time', 'installation_growth_level',
        'fertilizer_amount', 'npc_schedule_key',
        'init_state_hash', 'alias_name',
        'element_start', 'element_end',
        'broken_offset', 'lock_offset',
        'reason_offset', 'reset_time_offset',
    )

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, None)


class ConnectedActor:
    __slots__ = ('node_id', 'character_key', 'mercenary_no')

    def __init__(self):
        self.node_id = 0
        self.character_key = 0
        self.mercenary_no = 0


class QuestDeepData:

    def __init__(self):
        self.quests = []
        self.missions = []
        self.stages = []
        self.stage_map = {}
        self.gimmick_links = []
        self.gimmick_by_stage = {}
        self.quest_to_stages = {}
        self.quest_to_missions = {}
        self.quest_names = {}
        self.mission_names = {}
        self.stage_names = {}
        self.npc_blocks = []
        self.field_characters = []
        self.skills_learned = []
        self.friendships = []
        self.faction_spawns = []
        self.npc_schedules = []


def _read_scalar(raw, field):
    if not field.present:
        return None
    sz = field.end_offset - field.start_offset
    off = field.start_offset
    if sz == 1:
        return raw[off]
    elif sz == 2:
        return struct.unpack_from('<H', raw, off)[0]
    elif sz == 4:
        return struct.unpack_from('<I', raw, off)[0]
    elif sz == 8:
        return struct.unpack_from('<Q', raw, off)[0]
    elif sz == 12:
        return struct.unpack_from('<fff', raw, off)
    return raw[off:off + sz]


def parse_quest_deep(raw, result, quest_names=None, mission_names=None):
    data = QuestDeepData()
    data.quest_names = quest_names or {}
    data.mission_names = mission_names or {}

    quest_block = None
    for obj in result['objects']:
        if obj.class_name == 'QuestSaveData':
            quest_block = obj
            break

    if not quest_block:
        log.warning("No QuestSaveData block found")
        return data

    for field in quest_block.fields:
        if not field.present:
            continue

        if field.name == '_questStateList' and field.list_elements:
            _parse_quest_list(raw, field.list_elements, data)

        elif field.name == '_missionStateList' and field.list_elements:
            _parse_mission_list(raw, field.list_elements, data)

        elif field.name == '_stageStateData' and field.list_elements:
            _parse_stage_list(raw, field.list_elements, data)

        elif field.name == '_questGaugeStateList' and field.list_elements:
            pass

    for obj in result['objects']:
        if obj.class_name == 'FieldSaveData':
            _parse_field_gimmicks(raw, obj, data)

    for obj in result['objects']:
        if obj.class_name == 'FieldNPCSaveData':
            npc = {}
            for f in obj.fields:
                if f.present:
                    npc[f.name] = _read_scalar(raw, f)
            npc['_block_offset'] = obj.data_offset
            data.npc_blocks.append(npc)

    for obj in result['objects']:
        if obj.class_name == 'FactionSpawnStageManagerSaveData':
            for field in obj.fields:
                if not field.list_elements:
                    continue
                for elem in field.list_elements:
                    entry = {}
                    for cf in (getattr(elem, 'child_fields', None) or []):
                        if not cf.present:
                            continue
                        sz = cf.end_offset - cf.start_offset
                        if cf.name == '_factionNodeKey' and sz == 4:
                            entry['node_key'] = struct.unpack_from('<I', raw, cf.start_offset)[0]
                        elif cf.name == '_factionSpawnDataKey' and sz == 4:
                            entry['spawn_key'] = struct.unpack_from('<I', raw, cf.start_offset)[0]
                        elif cf.name == '_factionPatrolList':
                            entry['patrol_size'] = sz
                    data.faction_spawns.append(entry)

    for obj in result['objects']:
        if obj.class_name == 'NPCScheduleStageManagerSaveData':
            for field in obj.fields:
                if not field.list_elements:
                    continue
                for elem in field.list_elements:
                    entry = {}
                    for cf in (getattr(elem, 'child_fields', None) or []):
                        if not cf.present:
                            continue
                        if cf.name == '_stageNameHash':
                            entry['stage_hash'] = _read_scalar(raw, cf)
                        elif cf.name == '_characterList' and cf.list_elements:
                            entry['char_count'] = len(cf.list_elements)
                    data.npc_schedules.append(entry)

    for obj in result['objects']:
        if obj.class_name == 'FriendlySaveData':
            try:
                _parse_friendly_data(raw, obj, data)
            except Exception as e:
                log.warning("FriendlySaveData parse failed: %s", e)

    _build_gimmick_stage_map(data)
    load_pabgb_cross_refs(data)

    log.info("Parsed %d quests, %d missions, %d stages, %d gimmick links, %d NPCs, %d quest->stage xrefs",
             len(data.quests), len(data.missions), len(data.stages),
             len(data.gimmick_links), len(data.npc_blocks), len(data.quest_to_stages))

    return data


def _parse_quest_list(raw, elements, data):
    for elem in elements:
        q = QuestEntry()
        q.element_start = elem.start_offset
        q.element_end = elem.end_offset

        for cf in (elem.child_fields or []):
            if cf.name == '_questKey' and cf.present:
                q.key = _read_scalar(raw, cf)
            elif cf.name == '_state' and cf.present:
                q.state = _read_scalar(raw, cf)
                q.state_offset = cf.start_offset
                q.state_size = cf.end_offset - cf.start_offset
            elif cf.name == '_completedTime' and cf.present:
                q.completed_time = _read_scalar(raw, cf)
            elif cf.name == '_branchedTime' and cf.present:
                q.branched_time = _read_scalar(raw, cf)
            elif cf.name == '_delayedFromQuestKey' and cf.present:
                q.delayed_from_quest = _read_scalar(raw, cf)
            elif cf.name == '_delayTime' and cf.present:
                q.delay_time = _read_scalar(raw, cf)

        q.mask_hex = elem.child_mask_bytes.hex() if elem.child_mask_bytes else ''
        q.name = data.quest_names.get(q.key, f'Quest_{q.key}')
        data.quests.append(q)


def _parse_mission_list(raw, elements, data):
    for elem in elements:
        m = MissionEntry()
        m.element_start = elem.start_offset
        m.element_end = elem.end_offset

        for cf in (elem.child_fields or []):
            if cf.name == '_key' and cf.present:
                m.key = _read_scalar(raw, cf)
            elif cf.name == '_state' and cf.present:
                m.state = _read_scalar(raw, cf)
                m.state_offset = cf.start_offset
                m.state_size = cf.end_offset - cf.start_offset
            elif cf.name == '_completedTime' and cf.present:
                m.completed_time = _read_scalar(raw, cf)
            elif cf.name == '_branchedTime' and cf.present:
                m.branched_time = _read_scalar(raw, cf)
            elif cf.name == '_delayedTime' and cf.present:
                m.delayed_time = _read_scalar(raw, cf)
            elif cf.name == '_completeCount' and cf.present:
                m.complete_count = _read_scalar(raw, cf)

        m.mask_hex = elem.child_mask_bytes.hex() if elem.child_mask_bytes else ''
        m.name = data.mission_names.get(m.key, f'Mission_{m.key}')
        data.missions.append(m)


def _parse_stage_list(raw, elements, data):
    for elem in elements:
        s = StageEntry()
        s.element_start = elem.start_offset
        s.element_end = elem.end_offset

        for cf in (elem.child_fields or []):
            if not cf.present:
                continue
            val = _read_scalar(raw, cf)

            if cf.name == '_key':
                s.key = val
            elif cf.name == '_state':
                s.state = val
                s.state_offset = cf.start_offset
            elif cf.name == '_completeType':
                s.complete_type = val
            elif cf.name == '_isWaitBranch':
                s.is_wait_branch = val
            elif cf.name == '_isSkipComplete':
                s.is_skip_complete = val
            elif cf.name == '_completedCount':
                s.completed_count = val
            elif cf.name == '_completedTime':
                s.completed_time = val
            elif cf.name == '_delayedTime':
                s.delayed_time = val
            elif cf.name == '_branchedTime':
                s.branched_time = val
            elif cf.name == '_discoverPivotPosition':
                s.position = val
            elif cf.name == '_delayedFromMissionKey':
                s.delayed_from_mission = val
            elif cf.name == '_delayedFromStageKey':
                s.delayed_from_stage = val
            elif cf.name == '_connectCharacterList' and cf.list_elements:
                for actor_elem in cf.list_elements:
                    actor = ConnectedActor()
                    for af in (actor_elem.child_fields or []):
                        if not af.present:
                            continue
                        if af.name == '_nodeId':
                            actor.node_id = _read_scalar(raw, af)
                        elif af.name == '_characterKey':
                            actor.character_key = _read_scalar(raw, af)
                        elif af.name == '_mercenaryNo':
                            actor.mercenary_no = _read_scalar(raw, af)
                    s.connected_actors.append(actor)

        data.stages.append(s)
        if s.key:
            data.stage_map[s.key] = s


def _parse_field_gimmicks(raw, field_save_obj, data):
    for field in field_save_obj.fields:
        if field.name != '_fieldGimmickSaveDataList' or not field.list_elements:
            continue

        for elem in field.list_elements:
            g = GimmickLink()
            g.element_start = elem.start_offset
            g.element_end = elem.end_offset

            for cf in (elem.child_fields or []):
                if not cf.present:
                    continue
                val = _read_scalar(raw, cf)
                n = cf.name

                if n == '_fieldGimmickSaveDataKey':
                    g.gimmick_key = val
                elif n == '_gimmickInfoKey':
                    g.gimmick_info_key = val
                elif n == '_stageKey':
                    g.stage_key = val
                elif n == '_saveRootFieldGimmickSaveDataKey':
                    g.save_root_key = val
                elif n == '_ownerLevelName':
                    g.owner_level_name = val
                elif n == '_isBroken':
                    g.is_broken = val
                    g.broken_offset = cf.start_offset
                elif n == '_isLockState':
                    g.is_lock_state = val
                    g.lock_offset = cf.start_offset
                elif n == '_saveByCheat':
                    g.save_by_cheat = val
                elif n == '_isSpreadBroken':
                    g.is_spread_broken = val
                elif n == '_isLogoutFromGimmick':
                    g.is_logout_from_gimmick = val
                elif n == '_isLogoutedAwayFromOriginTransform':
                    g.is_logout_away = val
                elif n == '_isActivateAwayFromOriginTransform':
                    g.is_activate_away = val
                elif n == '_isRaiseGamePlayLevelGimmickComplete':
                    g.is_raise_complete = val
                elif n == '_fieldSaveDataReason':
                    g.field_save_reason = val
                    g.reason_offset = cf.start_offset
                elif n == '_spawnReason':
                    g.spawn_reason = val
                elif n == '_spawnStyle':
                    g.spawn_style = val
                elif n == '_resetTimeSecondsOfDays':
                    g.reset_time = val
                    g.reset_time_offset = cf.start_offset
                elif n == '_leftDropRollCount':
                    g.left_drop_roll_count = val
                elif n == '_installationTime':
                    g.installation_time = val
                elif n == '_installationGrowthLevel':
                    g.installation_growth_level = val
                elif n == '_fertilizerAmount':
                    g.fertilizer_amount = val
                elif n == '_npcScheduleKey':
                    g.npc_schedule_key = val
                elif n == '_initStateNameHash':
                    g.init_state_hash = val
                elif n == '_aliasName':
                    g.alias_name = val

            data.gimmick_links.append(g)


def _parse_friendly_data(raw, obj, data):
    for field in obj.fields:
        if not field.present:
            continue

        if field.name == '_skillLearnSaveDataList' and field.list_elements:
            for elem in field.list_elements:
                try:
                    skill = {'key': 0, 'artifact_count': 0, 'key_offset': None, 'count_offset': None}
                    for cf in (getattr(elem, 'child_fields', None) or []):
                        if not cf.present:
                            continue
                        if cf.name == '_knowledgeKey':
                            skill['key'] = _read_scalar(raw, cf)
                            skill['key_offset'] = cf.start_offset
                        elif cf.name == '_usedArtifactCount':
                            skill['artifact_count'] = _read_scalar(raw, cf)
                            skill['count_offset'] = cf.start_offset
                    data.skills_learned.append(skill)
                except Exception:
                    continue

        elif field.name == '_friendlyDataList' and field.list_elements:
            for elem in field.list_elements:
                try:
                    friend = {'character_key': 0, 'key_offset': None,
                              'threat_rewarded': False, 'read_memory_rewarded': False}
                    for cf in (getattr(elem, 'child_fields', None) or []):
                        if not cf.present:
                            continue
                        if cf.name == '_characterKey':
                            friend['character_key'] = _read_scalar(raw, cf)
                            friend['key_offset'] = cf.start_offset
                        elif cf.name == '_threatRewarded':
                            friend['threat_rewarded'] = bool(_read_scalar(raw, cf))
                        elif cf.name == '_readMemoryRewarded':
                            friend['read_memory_rewarded'] = bool(_read_scalar(raw, cf))
                        elif cf.name == '_levelData' and getattr(cf, 'child_fields', None):
                            for lf in (cf.child_fields or []):
                                if lf.present and lf.name:
                                    friend[lf.name] = _read_scalar(raw, lf)
                    data.friendships.append(friend)
                except Exception:
                    continue


def _build_gimmick_stage_map(data):
    for g in data.gimmick_links:
        if g.stage_key and g.stage_key != 0:
            if g.stage_key not in data.gimmick_by_stage:
                data.gimmick_by_stage[g.stage_key] = []
            data.gimmick_by_stage[g.stage_key].append(g)


def load_pabgb_cross_refs(data, quest_stages_path=None):
    import sys
    exe_dir = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, '_MEIPASS', None):
        exe_dir = sys._MEIPASS

    map_path = quest_stages_path or os.path.join(exe_dir, 'quest_stage_map.json')
    if os.path.exists(map_path):
        try:
            with open(map_path, 'r', encoding='utf-8') as f:
                qmap = json.load(f)
            qs = qmap.get('quest_stages', {})
            qm = qmap.get('quest_missions', {})
            for k, v in qs.items():
                data.quest_to_stages[int(k)] = v
            for k, v in qm.items():
                data.quest_to_missions[int(k)] = v
            log.info("Loaded quest cross-refs: %d quest->stage, %d quest->mission",
                     len(qs), len(qm))
            return
        except Exception as e:
            log.warning("Failed to load quest_stage_map.json: %s", e)

    try:
        import questinfo_parser
        import crimson_rs
        game_path = 'C:/Program Files (x86)/Steam/steamapps/common/Crimson Desert'
        dp = 'gamedata/binary__/client/bin'
        body = crimson_rs.extract_file(game_path, '0008', dp, 'questinfo.pabgb')
        gh = crimson_rs.extract_file(game_path, '0008', dp, 'questinfo.pabgh')
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pabgb', delete=False) as f:
            f.write(body); pb = f.name
        with tempfile.NamedTemporaryFile(suffix='.pabgh', delete=False) as f:
            f.write(gh); pg = f.name
        entries, failures = questinfo_parser.parse_all(pb, pg)
        for e in entries:
            if e['stages']:
                data.quest_to_stages[e['key']] = e['stages']
            if e['missions']:
                data.quest_to_missions[e['key']] = e['missions']
        log.info("Live-parsed quest cross-refs: %d entries (%d failures)", len(entries), failures)
        os.unlink(pb)
        os.unlink(pg)
        return
    except Exception as e:
        log.debug("Live questinfo extraction failed: %s", e)

    dump_dir = os.path.join(exe_dir, 'pabgb_full_dump')
    quest_dump = os.path.join(dump_dir, 'questinfo.json')
    if os.path.exists(quest_dump):
        try:
            with open(quest_dump, 'r', encoding='utf-8') as f:
                quests = json.load(f)
            for q in quests:
                qkey = q.get('_key')
                if not qkey:
                    continue
                stage_list = q.get('_stageList')
                mission_list = q.get('_missionList')
                if isinstance(stage_list, list):
                    data.quest_to_stages[qkey] = stage_list
                if isinstance(mission_list, list):
                    data.quest_to_missions[qkey] = mission_list
        except Exception as e:
            log.warning("Failed to load questinfo.json dump: %s", e)


def get_stages_for_quest(data, quest_key):
    stage_keys = data.quest_to_stages.get(quest_key, [])
    if stage_keys:
        return [data.stage_map[k] for k in stage_keys if k in data.stage_map]

    return []


def get_gimmicks_for_stage(data, stage_key):
    return data.gimmick_by_stage.get(stage_key, [])


def get_gimmicks_for_quest(data, quest_key):
    gimmicks = []
    for stage in get_stages_for_quest(data, quest_key):
        gimmicks.extend(get_gimmicks_for_stage(data, stage.key))
    return gimmicks


def summarize(data):
    stage_states = {}
    for s in data.stages:
        stage_states[s.state] = stage_states.get(s.state, 0) + 1

    quest_states = {}
    for q in data.quests:
        quest_states[q.state] = quest_states.get(q.state, 0) + 1

    gimmick_with_stage = sum(1 for g in data.gimmick_links if g.stage_key and g.stage_key != 0)
    actors = sum(len(s.connected_actors) for s in data.stages)

    lines = [
        f"Quests: {len(data.quests)} (states: {quest_states})",
        f"Missions: {len(data.missions)}",
        f"Stages: {len(data.stages)} (states: {stage_states})",
        f"Gimmick links: {len(data.gimmick_links)} ({gimmick_with_stage} with stage key)",
        f"Connected actors: {actors}",
        f"NPC blocks: {len(data.npc_blocks)}",
        f"Quest→Stage cross-refs: {len(data.quest_to_stages)}",
    ]
    return '\n'.join(lines)
