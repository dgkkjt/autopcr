"""Source selection and bounded fallback for the two counterattack modules."""

import asyncio
import math
import os


async def query_counterattack(task, available, defend, multi=False, account_data=None):
    from .arena import instance as pcrdfans

    if multi and len(defend) not in (2, 3):
        from ..model.error import AbortError
        raise AbortError("pjjc回刺需要两支或三支已知防守队伍")

    key = "opponent_pjjc_query_source" if multi else "opponent_jjc_query_source"
    available = frozenset(available)
    if task.get_config(key) == "mzq":
        safe_errors = ()
        try:
            from .arena_training import build_snapshot, TrainingError
            from .arena_mzq_service import RecommendationError
            safe_errors = (TrainingError, RecommendationError)
            from .arena_mzq import get_attack, get_multi_attack
            snapshot = build_snapshot(account_data, available)
            task._log("查询来源：mzq（我方账号练度模拟，保留自动五星；敌方满练；EX沿用模拟器配置；10次至少8胜）")
            result = await (get_multi_attack(available, defend, snapshot) if multi
                            else get_attack(available, defend, snapshot))
            if result:
                return result
            reason = "没有完整可用方案"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Only our explicitly sanitized errors are suitable for user logs.
            # Hoshino imports this package with a plugin prefix. Identify our
            # sanitized exceptions by class, independently of that package name.
            reason = str(error) if isinstance(error, safe_errors) else type(error).__name__
        task._log(f"mzq未返回可用方案（{reason}），降级使用PCRDFans")
    task._log("查询来源：PCRDFans")
    try:
        seconds = float(os.getenv("AUTOPCR_ARENA_PCRDFANS_TIMEOUT", "30"))
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError()
    except ValueError:
        from ..model.error import AbortError
        raise AbortError("AUTOPCR_ARENA_PCRDFANS_TIMEOUT 必须为正数") from None
    try:
        if multi:
            return await asyncio.wait_for(pcrdfans.get_multi_attack(available, defend, limit=9, strict=True), seconds)
        return await asyncio.wait_for(pcrdfans.get_attack(available, defend, strict=True), seconds)
    except asyncio.TimeoutError:
        from ..model.error import AbortError
        raise AbortError("PCRDFans查询阶段超时，未自动编队") from None
