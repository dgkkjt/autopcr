"""Immutable account training snapshots used only by counterattack simulation."""

import hashlib
import json
from dataclasses import dataclass

from pydantic import PrivateAttr

from ..model.custom import ArenaQueryResult


class TrainingError(Exception):
    """Account data cannot be represented faithfully by the simulator protocol."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def integer(value, name, minimum=0, maximum=None):
    # Game enums are IntEnum, but bool is not an integer stat.
    if not isinstance(value, int) or isinstance(value, bool):
        raise TrainingError(f"账号练度：{name}缺失或不是整数")
    value = int(value)
    if value < minimum or (maximum is not None and value > maximum):
        raise TrainingError(f"账号练度：{name}超出有效范围")
    return value


def needs_five_star_change(unit):
    return unit.unit_rarity == 5 and unit.battle_rarity != 0


def effective_rarity(unit):
    owned = integer(unit.unit_rarity, '持有星级', 1, 6)
    # UnitData may omit an unset battle_rarity. The existing attribute/power
    # calculation also uses owned rarity for None/0; no override is active.
    battle = integer(0 if unit.battle_rarity is None else unit.battle_rarity, '战斗星级', 0, 6)
    if battle > owned:
        raise TrainingError('账号练度：战斗星级高于持有星级')
    return 5 if owned == 5 else (battle or owned)


def skill_level(items, index, name, preferred_ids=()):
    if items is None or len(items) <= index:
        raise TrainingError(f'账号练度：{name}数据缺失')
    for skill_id in preferred_ids:
        matches = [item for item in items if skill_id and getattr(item, 'skill_id', None) == skill_id]
        if matches:
            if len({item.skill_level for item in matches}) != 1:
                raise TrainingError(f'账号练度：{name}生效形态等级不明确')
            return integer(matches[0].skill_level, name)
    if preferred_ids and any(getattr(item, 'skill_id', None) is not None for item in items):
        raise TrainingError(f'账号练度：{name}技能ID与生效形态不匹配')
    slots = [getattr(item, 'slot_number', None) for item in items]
    if all(isinstance(slot, int) and not isinstance(slot, bool) for slot in slots):
        offset = (min(slots) // 100 * 100 + 1 if min(slots) >= 100
                  else 0 if min(slots) == 0 else 1)
        matches = [item for item in items if item.slot_number == index + offset]
        if not matches or len({item.skill_level for item in matches}) != 1:
            raise TrainingError(f'账号练度：{name}槽位不明确')
        item = matches[0]
    else:
        raise TrainingError(f'账号练度：{name}槽位数据缺失')
    return integer(item.skill_level, name)


def love_dictionary(data, database, uid):
    if data.read_story_ids is None or data.unit_love_data is None:
        raise TrainingError('账号练度：好感或已读剧情数据缺失')
    read = set(data.read_story_ids)
    grouped = {}
    for status in database.chara2story.get(uid, []):
        if not any(getattr(status, f'status_rate_{i}', 0) for i in range(1, 6)):
            continue
        detail = database.story_detail.get(status.story_id)
        if detail is None:
            raise TrainingError('账号练度：羁绊剧情数据库缺失')
        owner = integer(detail.requirement_id, '羁绊角色ID', 1)
        level = integer(detail.love_level, '剧情好感门槛', 0, 12)
        grouped.setdefault(owner, []).append((level, status.story_id in read))
    result = {}
    for owner, stories in grouped.items():
        achieved = max((level for level, is_read in stories if is_read), default=0)
        love = data.unit_love_data.get(owner // 100)
        if achieved and (love is None or integer(love.love_level, '好感等级', 0, 12) < achieved):
            raise TrainingError('账号练度：已读剧情与好感数据不一致')
        if any(level <= achieved and not is_read for level, is_read in stories):
            raise TrainingError('账号练度：非连续羁绊不能用模拟器好感等级准确表示')
        result[str(owner)] = achieved
    return result


def unit_payload(data, database, uid):
    unit = data.unit[uid]
    if unit.id != uid:
        raise TrainingError('账号练度：角色数据ID不一致')
    equipment = unit.equip_slot
    if equipment is None or len(equipment) != 6:
        raise TrainingError('账号练度：普通装备必须包含六个槽位')
    equip_levels = []
    for slot in equipment:
        if slot.is_slot not in (True, False) or slot.is_slot is None:
            raise TrainingError('账号练度：装备穿戴状态缺失')
        equip_levels.append(integer(slot.enhancement_level, '装备强化', 0, 5) if slot.is_slot else -1)
    unique = unit.unique_equip_slot
    if unique is None:
        raise TrainingError('账号练度：专武数据缺失')

    def unique_level(index, absent):
        if len(unique) <= index:
            return absent
        slot = unique[index]
        if slot.is_slot is None or slot.is_slot not in (True, False):
            raise TrainingError('账号练度：专武穿戴状态缺失')
        return integer(slot.enhancement_level, '专武强化', 0) if slot.is_slot else absent

    rarity = effective_rarity(unit)
    definition = database.unit_skill_data.get(uid)
    if definition is None:
        raise TrainingError('账号练度：技能形态数据库缺失')

    def skill(items, index, name, base, evolved, active):
        base_id = getattr(definition, base, 0)
        evolved_id = getattr(definition, evolved, 0) if active else 0
        return skill_level(items, index, name, (evolved_id, base_id))

    return {
        'unitId': uid, 'level': integer(unit.unit_level, '等级', 1),
        'rank': integer(unit.promotion_level, 'Rank', 1), 'rarity': rarity,
        'equipLevel': equip_levels,
        'skillLevel': [skill(unit.union_burst, 0, 'UB', 'union_burst', 'union_burst_evolution', rarity == 6),
                       skill(unit.main_skill, 0, 'S1', 'main_skill_1', 'main_skill_evolution_1', unique_level(0, 0) > 0),
                       skill(unit.main_skill, 1, 'S2', 'main_skill_2', 'main_skill_evolution_2', unique_level(1, -1) >= 0),
                       skill(unit.ex_skill, 0, 'EX被动', 'ex_skill_1', 'ex_skill_evolution_1', rarity >= 5)],
        'uniqueEqLv': unique_level(0, 0), 'uniqueEq2Lv': unique_level(1, -1),
        'playLoveDic': love_dictionary(data, database, uid),
    }


def progression_payload(data, database):
    knight = data.princess_knight_info
    if (knight is None or knight.talent_level_info_list is None
            or knight.talent_skill_last_enhanced_page_node_list is None or data.unit_role_list is None):
        raise TrainingError('账号练度：属性、节点或职能数据缺失，请刷新账号数据')
    talents = {}
    for item in knight.talent_level_info_list:
        key = integer(item.talent_id, '属性ID', 1)
        talents[str(key)] = integer(database.get_talent_level(integer(item.total_point, '属性点数')), '属性等级')
    nodes = {str(integer(item.node_id, '属性节点ID', 1)): integer(item.enhance_level, '属性节点等级')
             for item in knight.talent_skill_last_enhanced_page_node_list}
    latest = knight.team_skill_latest_node
    if latest is None:
        raise TrainingError('账号练度：大师节点状态缺失，请刷新账号数据')
    master_id = integer(latest.node_id, '大师节点ID')
    master = ({str(master_id): integer(latest.enhance_level, '大师节点等级')} if master_id else {})
    roles = {}
    for item in data.unit_role_list:
        role = integer(item.unit_role_id, '职能ID', 1)
        for slot in range(1, 5):
            level = integer(getattr(item, f'slot_level_{slot}'), '职能槽位等级')
            enhance = integer(getattr(item, f'enhance_level_{slot}'), '职能强化等级', -1)
            if level > 0 and enhance >= 0:
                roles[str(role * 100 + slot)] = {'Item1': level, 'Item2': enhance}
    return {'TalentSkillLevels': talents, 'TalentSkillNodeLevels': nodes,
            'MasterSkillNodeLevels': master, 'RoleMastery': roles}


@dataclass(frozen=True)
class TrainingSnapshot:
    units_json: str
    progression_json: str
    requested_ids: tuple
    errors: tuple = ()

    @property
    def units(self):
        return {int(uid): value for uid, value in json.loads(self.units_json).items()}

    @property
    def progression(self):
        return json.loads(self.progression_json)

    @property
    def fingerprint(self):
        return digest([self.units_json, self.progression_json])

    def matches(self, data, database=None):
        try:
            current = build_snapshot(data, self.requested_ids, database)
            return current.fingerprint == self.fingerprint
        except (TrainingError, AttributeError, KeyError, TypeError, ValueError):
            return False


class ActualArenaQueryResult(ArenaQueryResult):
    _training_snapshot = PrivateAttr(default=None)


def build_snapshot(data, available, database=None):
    if database is None:
        from ..db.database import db
        database = db
    if data is None or data.unit is None:
        raise TrainingError('账号练度：缺少本次账号数据')
    progression = progression_payload(data, database)
    units, errors = {}, []
    requested = tuple(sorted(available))
    for uid in requested:
        try:
            units[str(uid)] = unit_payload(data, database, uid)
        except (TrainingError, AttributeError, KeyError, TypeError, ValueError) as error:
            reason = str(error) if isinstance(error, TrainingError) else '字段缺失或类型不正确'
            errors.append(f'{uid}：{reason}')
    if errors:
        raise TrainingError('账号练度：数据不完整或无法准确表示；' + '；'.join(errors[:3]))
    if len(units) < 5:
        raise TrainingError('账号练度：可准确转换的角色不足5人；' + '；'.join(errors[:3]))
    return TrainingSnapshot(canonical(units), canonical(progression), requested, tuple(errors))


EX_FIELDS = ('exEquip', 'exEquipLevel', 'exEquipSubs')
CORE_FIELDS = ('unitId', 'level', 'rarity', 'rank', 'equipLevel', 'skillLevel',
               'uniqueEqLv', 'uniqueEq2Lv', 'playLoveDic')


def validate_template(unit):
    if not isinstance(unit, dict) or any(key not in unit for key in CORE_FIELDS + EX_FIELDS):
        raise TrainingError('满练预热：返回的角色配置不完整')
    integer(unit['unitId'], '模板角色ID', 1)
    for field in ('level', 'rank'):
        integer(unit[field], '模板' + field, 2)
    integer(unit['rarity'], '模板星级', 5, 6)
    for field, size, minimum in (('equipLevel', 6, -1), ('skillLevel', 4, 0),
                                  ('exEquip', 3, 0), ('exEquipLevel', 3, 0)):
        if not isinstance(unit[field], list) or len(unit[field]) != size:
            raise TrainingError('满练预热：模板数组长度不正确')
        for value in unit[field]:
            integer(value, '模板' + field, minimum)
    if any(value != unit['level'] for value in unit['skillLevel']):
        raise TrainingError('满练预热：技能等级未按满练处理')
    integer(unit['uniqueEqLv'], '模板专武一', -1)
    integer(unit['uniqueEq2Lv'], '模板专武二', -1)
    if not isinstance(unit['playLoveDic'], dict) or not isinstance(unit['exEquipSubs'], dict):
        raise TrainingError('满练预热：羁绊或EX副属性格式不正确')
    if set(unit['exEquipSubs']) != {'0', '1', '2'}:
        raise TrainingError('满练预热：EX副属性必须完整包含三个槽位')
    for key, value in unit['playLoveDic'].items():
        if not str(key).isdigit():
            raise TrainingError('满练预热：羁绊角色ID无效')
        integer(value, '模板羁绊', 0, 12)
    for key, values in unit['exEquipSubs'].items():
        if str(key) not in ('0', '1', '2') or not isinstance(values, list) or len(values) != 8:
            raise TrainingError('满练预热：EX副属性数组无效')
        for value in values:
            integer(value, '模板EX副属性')
    return json.loads(canonical(unit))


def actual_player(snapshot, uid, template, units=None):
    player = json.loads(canonical((snapshot.units if units is None else units)[uid]))
    player.update({field: json.loads(canonical(template[field])) for field in EX_FIELDS})
    return player


def assert_formal_units(item, players, enemies):
    for name, expected, fields in (('PlayerList', players, CORE_FIELDS + EX_FIELDS),
                                   ('EnemyList', enemies, CORE_FIELDS + EX_FIELDS)):
        rows = item.get(name) if isinstance(item, dict) else None
        if not isinstance(rows, list) or len(rows) != len(expected) or any(not isinstance(row, dict) for row in rows):
            raise TrainingError('实际练度校验：模拟结果缺少完整的' + name)
        actual = {row.get('unitId'): row for row in rows}
        if len(actual) != len(expected):
            raise TrainingError('实际练度校验：模拟结果包含重复角色')
        for unit in expected:
            row = actual.get(unit['unitId'], {})
            for field in fields:
                expected_value, actual_value = unit.get(field), row.get(field)
                if field == 'playLoveDic' and isinstance(actual_value, dict):
                    # Json.NET may retain zero-valued defaults initialized by unitId.
                    actual_value = {str(k): v for k, v in actual_value.items() if v != 0}
                    expected_value = {str(k): v for k, v in expected_value.items() if v != 0}
                if canonical(actual_value) != canonical(expected_value):
                    raise TrainingError(f'实际练度校验：{name}的{field}与请求不一致')
