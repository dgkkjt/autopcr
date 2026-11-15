"""Public counterattack adapters; shared JJC orchestration is intentionally unused."""

from .arena_mzq_service import get_service


async def get_attack(available_unit, defend):
    return [group[0] for group in await get_service().query(available_unit, [defend])]


async def get_multi_attack(available_unit, defends):
    if len(defends) not in (2, 3):
        from .arena_mzq_service import RecommendationError
        raise RecommendationError("防守输入：pjjc需要两支或三支完整防守")
    return await get_service().query(available_unit, defends)
