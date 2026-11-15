"""Preflight and observed write outcomes for both counterattack sources."""

import asyncio

from ..core.apiclient import ApiException
from ..model.common import ChangeRarityUnit, DeckListData
from ..model.enums import ePartyType
from ..model.requests import LoadIndexRequest
from .arena_support import valid_team


class DeckReadbackError(Exception):
    """Readback diagnostics containing field names, never raw account responses."""


def deck_units(deck):
    if deck is None:
        raise ValueError("未返回进攻队伍")
    values = [getattr(deck, f"unit_id_{i}", None) for i in range(1, 6)]
    invalid = [f"unit_id_{i}" for i, value in enumerate(values, 1)
               if type(value) is not int or value < 0]
    if invalid:
        raise ValueError("进攻队伍缺少有效槽位字段：" + ",".join(invalid))
    return values


async def read_arena_attack_deck(client):
    """Prefer arena/info; missing deck data requires an independent fresh read.

    client.data.deck_list cannot verify a write: DeckUpdateResponse updates that
    local cache from the submitted request, even without echoing server state.
    """
    try:
        info = await client.get_arena_info()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise DeckReadbackError(f"arena/info 读取失败（{type(error).__name__}）") from None

    try:
        return deck_units(getattr(info, "attack_deck", None))
    except ValueError:
        pass

    try:
        fresh = await client.request(LoadIndexRequest(carrier="OPPO"))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise DeckReadbackError(
            f"arena/info 未返回完整进攻队伍；load/index 读取失败（{type(error).__name__}）"
        ) from None

    decks = [deck for deck in (getattr(fresh, "deck_list", None) or [])
             if getattr(deck, "deck_number", None) == ePartyType.ARENA]
    if len(decks) != 1:
        raise DeckReadbackError("arena/info 未返回完整进攻队伍；load/index 缺少唯一的竞技场进攻队伍")
    try:
        return deck_units(decks[0])
    except ValueError as error:
        raise DeckReadbackError("load/index：" + str(error)) from None


async def update_counterattack_deck(task, client, recommendations, pjjc=False):
    from ..db.database import db

    groups = recommendations if pjjc else [recommendations]
    teams = [[unit.id for unit in result.atk] for result in groups]
    available = frozenset(uid for uid, unit in client.data.unit.items() if unit.promotion_level >= 7)
    if (len(teams) not in ((2, 3) if pjjc else (1,))
            or any(not valid_team(team, available) for team in teams)
            or len({uid for team in teams for uid in team}) != sum(map(len, teams))):
        task._warn("编队预检失败：人数、持有情况、品级或跨队重复不符合要求；仅展示推荐")
        return
    teams = [db.deck_sort_unit(team) for team in teams]
    if pjjc and len(teams) == 2:
        used = {uid for team in teams for uid in team}
        # Filler has no predicted strength requirement. Prefer eligible roles,
        # but any owned, unused role can occupy the third team.
        remaining = sorted((uid for uid in client.data.unit if type(uid) is int and uid > 0 and uid not in used),
                           key=lambda uid: (uid not in available, uid))
        if not remaining:
            task._warn("第三队凑解失败：没有未被前两队占用的自有角色；仅展示推荐，不自动编队")
            return
        third = db.deck_sort_unit(remaining[:5] if len(remaining) >= 5 else remaining[:1])
        if (len(third) not in (1, 5) or len(set(third)) != len(third)
                or any(uid not in remaining for uid in third)):
            task._warn("第三队凑解预检失败；仅展示推荐，不自动编队")
            return
        teams.append(third + [0] * (5 - len(third)))
        task._log("第三队（凑解，未验证胜率）：" + " ".join(db.get_unit_name(uid) for uid in third))

    # Only recommended teams get automatic star changes; filler retains its stars.
    new_ids = [unit.id for result in groups for unit in result.atk]
    changes = [uid for uid in new_ids if client.data.unit[uid].unit_rarity == 5
               and client.data.unit[uid].battle_rarity != 0]
    if changes:
        names = "|".join(db.get_unit_name(uid) for uid in changes)
        try:
            await client.unit_change_rarity([ChangeRarityUnit(unit_id=uid, battle_rarity=5) for uid in changes])
            task._log(f"星级调整：确认成功，将{names}调至5星")
        except asyncio.CancelledError:
            task._warn("星级调整：结果未知（请求已取消），未继续提交编队")
            raise
        except ApiException:
            task._warn("星级调整：服务端返回失败，未继续提交编队")
            return
        except Exception:
            task._warn("星级调整：结果未知，正在读回确认")
            try:
                response = await client.request(LoadIndexRequest(carrier="OPPO"))
                units = {unit.id: unit for unit in (response.unit_list or [])}
                if not all(uid in units and units[uid].battle_rarity in (0, 5) for uid in changes):
                    task._warn("星级调整：读回未确认全部目标星级，未提交编队；已发生的部分变化不会自动回滚")
                    return
                task._log(f"星级调整：读回确认目标星级已生效，角色：{names}")
            except asyncio.CancelledError:
                raise
            except Exception:
                task._warn("星级调整：读回失败，结果仍未知；未提交编队，不自动重试或回滚")
                return

    under_rank = [uid for uid in new_ids if client.data.unit[uid].promotion_level < db.equip_max_rank - 1]
    if under_rank:
        task._warn("无品级加成：" + "，".join(db.get_unit_name(uid) for uid in under_rank))

    acknowledged = False
    try:
        if pjjc:
            await client.deck_update_list([
                DeckListData(deck_number=getattr(ePartyType, f"GRAND_ARENA_{i + 1}"), unit_list=team)
                for i, team in enumerate(teams)
            ])
        else:
            await client.deck_update(ePartyType.ARENA, teams[0])
        acknowledged = True
        task._log("编队提交：接口确认成功，正在读回队伍")
    except asyncio.CancelledError:
        task._warn("编队提交：结果未知（请求已取消）；已确认的星级调整不会自动回滚")
        raise
    except ApiException:
        task._warn("编队提交：服务端返回失败；已确认的星级调整不会自动回滚")
        return
    except Exception:
        task._warn("编队提交：结果未知，正在读回确认；不自动重试或回滚")

    try:
        if pjjc:
            info = await client.get_grand_arena_info()
            current = {deck.deck_number: deck for deck in (info.attack_deck_list or [])}
            observed = [deck_units(current[getattr(ePartyType, f"GRAND_ARENA_{i + 1}")])
                        for i in range(len(teams))]
        else:
            observed = [await read_arena_attack_deck(client)]
        if observed == teams:
            task._log("编队读回：确认目标队伍已生效")
        else:
            task._warn("编队读回：当前队伍与目标不一致，可能存在并发修改；不自动重试或回滚")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        reason = str(error) if isinstance(error, DeckReadbackError) else type(error).__name__
        task._warn("编队读回失败：" + ("提交已确认，当前队伍未验证" if acknowledged else "提交结果仍未知")
                   + "；原因：" + reason)
