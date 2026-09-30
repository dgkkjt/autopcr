## Plan: 每日任务补刷

新增一个默认关闭的日常模块，在最终通用任务领奖前检查最新的“通关 20 次”每日任务；仅补刷缺少次数，优先选择最新活动的最后一张可扫荡普图，否则选择当前开放的最后一张可扫荡 N 图。复用既有领奖和扫荡接口，并且只消耗当前体力。

**Steps**
1. 在 `d:\Documents\GitHub\autopcr\autopcr\module\modules\daily.py` 新增体力消耗模块（建议名称“补刷每日任务关卡”），默认关闭。通过 `client.mission_index()` 取得当前任务，从 `db.daily_mission_data` 中筛选主数据 `mission_condition == 1008` 且 `condition_num == 20` 的任务，并选取 `daily_mission_id` 最大的候选；仅在该任务尚未完成且未领奖时计算 `20 - clear_num`。
2. 在同一模块内实现目标关卡选择：若 `db.get_active_event()` 非空，选择 `event_id` 最大的活动，调用既有 `prepare_event_quest()` 更新活动关卡状态，再从 `db.get_event_normal_quests(event_id)` 倒序选择第一张三星可扫荡的普图；若没有活动，则从开始时间已到的 `db.normal_quest_data` 按关卡 ID 倒序选择第一张 `client.data.is_quest_sweepable()` 的 N 图。末关不可扫荡时向前回退；没有可扫荡关卡时记录警告并结束该模块。
3. 对选中的关卡调用 `client.quest_skip_aware(quest_id, missing_count, recover=False)`，确保只使用当前体力。记录目标任务、关卡、缺少次数和实际扫荡次数；若返回体力不足或实际次数小于差额，调用 `_warn` 记录未完成次数但不抛出中止异常，使后续日常继续执行。
4. 对任务不存在、任务已完成或已领取、没有目标关卡等无操作场景抛出带原因的 `SkipError`；保留 API 与扫荡异常的正常传播，避免把账号/接口异常误报为完成。不要在新模块中领取奖励。
5. 在 `d:\Documents\GitHub\autopcr\autopcr\module\modules\__init__.py` 的 `daily_modules` 中，将新模块插入 `mission_receive_last` 之前。现有 `mission_receive_last` 紧随其后重新读取任务并领取刚完成的每日奖励；不改动活动任务领奖链路。
6. 为可测试性将任务筛选与关卡候选选择收敛为小型纯辅助方法或可替换依赖；新增针对候选筛选、活动优先、无活动主线回退、末关未三星回退、任务完成跳过、体力不足警告的测试。若仓库没有可用自动化测试入口，则至少以受控模拟 `pcrclient` 的轻量测试覆盖这些分支，并记录无法执行的环境依赖。

**Relevant files**
- `d:\Documents\GitHub\autopcr\autopcr\module\modules\daily.py` — 复用 `mission_receive`、新增补刷模块和任务/关卡选择逻辑。
- `d:\Documents\GitHub\autopcr\autopcr\module\modules\__init__.py` — 在最终通用任务领奖前注册模块。
- `d:\Documents\GitHub\autopcr\autopcr\db\database.py` — 复用 `daily_mission_data`、`get_active_event()` 和 `get_event_normal_quests()`；预计不需要改动。
- `d:\Documents\GitHub\autopcr\autopcr\core\pcrclient.py` — 复用 `mission_index()` 与 `quest_skip_aware()`；预计不需要改动。
- `d:\Documents\GitHub\autopcr\autopcr\core\datamgr.py` — 复用 `is_quest_sweepable()`；预计不需要改动。

**Verification**
1. 对任务筛选与关卡选择辅助逻辑运行新增的窄范围测试，验证只选择最大 ID 的 `1008/20` 每日任务，且忽略非每日/不同条件任务。
2. 以模拟客户端验证：活动优先最新 `event_id`、末关未三星向前回退；无活动时选择最高可扫荡开放 N 图。
3. 验证扫荡请求次数等于 `20 - clear_num`、`recover=False`，体力不足时模块为 WARNING 且后续 `mission_receive_last` 仍执行。
4. 运行项目可用的 Python 测试/静态检查；当前编辑器中 `daily.py` 与模块清单无诊断，数据库文件已有大量独立诊断，不将其作为本改动的验收信号。

**Decisions**
- 目标是通用每日任务 `mission_receive`，不是 Hatsune 活动任务领取。
- “最新任务”定义为候选每日任务中 `daily_mission_id` 最大者。
- 有多个活动时取 `event_id` 最大的活动；优先该活动最后一张可扫荡普图。
- 目标末关不可扫荡时向前回退至同类第一张可扫荡关卡。
- 补刷不自动氪体；体力不足记录警告并继续日常。
- 新模块默认关闭，以避免用户在未明确启用前新增体力消耗行为。
- 覆盖 seven 活动仅体现在统一的活动关卡选择；不读取 seven 活动任务定义。
