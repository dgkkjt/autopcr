from ..modulebase import *
from ..config import ConditionalExecutionWrapper, ConditionalNotExecutionClient
from ...core.pcrclient import pcrclient
from ...core.apiclient import apiclient
from ...model.error import *
from ...db.database import db
from ...model.enums import eMissionStatusType
from .hatsune import hatsune_h_sweep, all_in_hatsune, prepare_event_quest
from .autosweep import (
    lazy_normal_sweep, smart_hard_sweep, smart_shiori_sweep,
    mirai_very_hard_sweep, mirai_sp1_h_sweep, mirai_sp1_shiori_sweep,
    smart_very_hard_sweep, smart_sweep, last_normal_quest_sweep,
)


@description('先按既有配置执行扫荡，次数不足时再强制启用任务并忽略执行时间条件，仍完不成则优先扫荡最新活动的最后一张普图或者最新n图。补足职能券任务次数后停止。')
@name('完成职能券任务')
@default(True)
@tag_stamina_consume
class role_mission_get(Module):
    first_order = [
        lazy_normal_sweep,
        smart_very_hard_sweep,
        hatsune_h_sweep,
        smart_sweep,
        mirai_very_hard_sweep,
        smart_hard_sweep,
        smart_shiori_sweep,
        mirai_sp1_h_sweep,
        mirai_sp1_shiori_sweep,
        last_normal_quest_sweep,
        all_in_hatsune,
    ]

    second_order = [
        smart_very_hard_sweep,
        hatsune_h_sweep,
        smart_sweep,
        mirai_very_hard_sweep,
        smart_hard_sweep,
        smart_shiori_sweep,
        mirai_sp1_h_sweep,
        mirai_sp1_shiori_sweep,
        all_in_hatsune,
    ]

    async def get_target_quest(self, client: pcrclient) -> int:
        events = db.get_active_event()
        if events:
            event_id = max(event.event_id for event in events)
            await prepare_event_quest(client, event_id)
            event_quests = client.data.hatsune_quest_dict.get(event_id, {})
            for quest_id in reversed(db.get_event_normal_quests(event_id)):
                if quest_id in event_quests and event_quests[quest_id].clear_flag == 3:
                    return quest_id

        for quest_id, quest in sorted(db.normal_quest_data.items(), reverse=True):
            if db.parse_time(quest.start_time) <= apiclient.datetime and client.data.is_quest_sweepable(quest_id):
                return quest_id
        return 0

    async def receive_reward_if_available(self, client: pcrclient, mission_id: int) -> bool:
        top = await client.mission_index()
        mission = next((mission for mission in top.missions if mission.mission_id == mission_id), None)
        if mission is None or mission.mission_status != eMissionStatusType.EnableReceive:
            return False

        response = await client.mission_receive(1)
        reward = await client.serialize_reward_summary(response.rewards)
        self._log("领取了职能券任务奖励，获得了:\n" + reward)
        return True

    async def do_task(self, client: pcrclient):
        mission_data = db.VIP_mission
        if mission_data is None:
            raise ValueError("未找到职能券任务！")
        top = await client.mission_index()
        missions = [m for m in top.missions if m.mission_id == mission_data.daily_mission_id]
        if len(missions) == 0:
            mission = None
        elif len(missions) > 1:
            raise ValueError("职能券任务不唯一！")
        else:
            mission = missions[0]
        remain = mission_data.condition_num - (mission.clear_num or 0) if mission else mission_data.condition_num
        if mission and mission.mission_status == eMissionStatusType.EnableReceive:
            await self.receive_reward_if_available(client, mission_data.daily_mission_id)
            return
        if mission and (mission.mission_status != eMissionStatusType.NoClear or remain <= 0):
            raise SkipError("职能券任务已完成")

        with client.override_config({
            'quest_skip_remaining': remain,
        }):
            def get_remaining() -> int:
                remaining = client.quest_skip_remaining
                if remaining is None:
                    raise RuntimeError("职能券任务扫荡次数未初始化")
                return remaining

            for force, order in [(False, self.first_order), (True, self.second_order)]:
                if get_remaining() <= 0:
                    break
                for module in order:
                    if get_remaining() <= 0:
                        break
                    sweep = module(self._parent)
                    if force:
                        sweep.config_overrides[sweep.key] = True
                        for key, config in sweep.config.items():
                            if isinstance(config, ConditionalNotExecutionClient):
                                sweep.config_overrides[key] = []
                            elif isinstance(config, ConditionalExecutionWrapper):
                                sweep.config_overrides[key] = ['总是执行']

                    before = get_remaining()
                    with client.override_config({'quest_skip_no_stamina': False}):
                        result = await sweep.do_from(client)
                        clear_count = before - get_remaining()
                        if clear_count or client.quest_skip_no_stamina:
                            msg = f"{sweep.name}: 扫荡{clear_count}次"
                            if client.quest_skip_no_stamina:
                                msg += "，体力不足"
                            self._log(msg)
                    if result.status == eResultStatus.PANIC:
                        raise PanicError(f"{sweep.name}执行失败: {result.log}")
                    if result.status == eResultStatus.ERROR:
                        raise ValueError(f"{sweep.name}执行失败: {result.log}")

            if get_remaining() <= 0:
                await self.receive_reward_if_available(client, mission_data.daily_mission_id)
                return

            quest_id = await self.get_target_quest(client)
            if not quest_id:
                self._warn("没有可扫荡的活动普图或N图")
                return

            _, clear_count, no_stamina = await client.quest_skip_aware(
                quest_id, get_remaining(), recover=False
            )
            self._log(f"{db.get_quest_name(quest_id)}: 扫荡{clear_count}次")
            if no_stamina or get_remaining() > 0:
                self._warn(f"职能券任务还需扫荡{get_remaining()}次")
            else:
                await self.receive_reward_if_available(client, mission_data.daily_mission_id)
