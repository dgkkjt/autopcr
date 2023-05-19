from nonebot import MessageSegment
from hoshino.typing import MessageSegment

from .autopcr.module.modulebase import ModuleManager
from .util import draw

class Task():
    def __init__(self, alian, target, bot, ev, qid = None, gid = None):
        self.info = (alian, target, bot, ev, qid, gid)

    async def do_task(self):
        alian, target, bot, ev, qid, gid = self.info 
        mgr = ModuleManager(target)
        user_id = ev.user_id if ev else qid

        if ev:
            await bot.send(ev, f"[CQ:reply,id={ev.message_id}]开始为{alian}清理日常")
        else:
            await bot.send_group_msg(group_id = gid, message = f"【定时任务】开始为{alian}清理日常")
        try:
            resp = await mgr.do_task()
            img = await draw(resp, alian)
            if ev:
                await bot.send(ev, f"[CQ:reply,id={ev.message_id}]" + MessageSegment.image(f'file:///{img}'))
            else:
                await bot.send_group_msg(group_id = gid, message = "【定时任务】" + MessageSegment.image(f'file:///{img}'))
        except Exception as e:
            if ev:
                await bot.send(ev, f"[CQ:reply,id={ev.message_id}]" + str(e))
            else:
                await bot.send_group_msg(group_id = gid, message = "【定时任务】" + str(e))

        return user_id, target

