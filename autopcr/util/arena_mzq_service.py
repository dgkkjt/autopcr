"""Counterattack-only orchestration; never calls the shared JJC recommender."""

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

import aiohttp

from ..constants import CACHE_DIR, DATA_DIR
from ..model.custom import ArenaQueryComment, ArenaQueryResult, ArenaQueryType, ArenaQueryUnit
from .arena_support import top_combinations, valid_team
from .arena_training import (TrainingError, TrainingSnapshot, ActualArenaQueryResult,
                             actual_player, assert_formal_units, validate_template, digest as training_digest)

log = logging.getLogger(__name__)
VALIDATION_VERSION = 3
REPEAT_COUNT = 10
MIN_WINS = 8


class RecommendationError(Exception):
    """Sanitized stage-specific failure suitable for a user log."""


def configured_seconds(name, default):
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value) or value <= 0:
            raise ValueError()
        return value
    except ValueError:
        raise RecommendationError(f"配置：{name} 必须为正数") from None


@dataclass
class RequestState:
    deadline: float
    stop: Event

    def check(self):
        if self.stop.is_set() or time.monotonic() >= self.deadline:
            raise RecommendationError("超时：mzq阶段预算已耗尽")


@dataclass(frozen=True)
class Recommendation:
    attack: tuple
    defend: tuple
    samples: tuple
    completed_at: float

    @property
    def wins(self):
        return sum(status == 0 for status, _, _ in self.samples)

    @property
    def mean_win_seconds(self):
        durations = [frames / 60 for status, frames, _ in self.samples if status == 0]
        return sum(durations) / len(durations) if durations else float("inf")

    @property
    def rank(self):
        return -self.wins / REPEAT_COUNT, self.mean_win_seconds

    @property
    def legacy_rank(self):
        return -(self.wins / REPEAT_COUNT * 10_000_000 - self.mean_win_seconds)

    def result(self, snapshot, cached=False):
        losses = sum(status == 1 for status, _, _ in self.samples)
        timeouts = sum(status == 2 for status, _, _ in self.samples)
        stamp = datetime.fromtimestamp(self.completed_at).strftime("%Y-%m-%d %H:%M:%S")
        result = ActualArenaQueryResult(
            atk=[ArenaQueryUnit(id=uid, equip=False, star=snapshot.units[uid]['rarity']) for uid in self.attack],
            up=self.wins, down=REPEAT_COUNT - self.wins, query_type=ArenaQueryType.MZQ,
            comment=[ArenaQueryComment(id="mzq", date="", avatar=0, msg=(
                f"角色满练；Rank、深域及精通按账号；保留自动五星；敌方满练；EX沿用模拟器配置；胜率{self.wins / REPEAT_COUNT:.0%}；"
                f"{self.wins}胜({self.mean_win_seconds:.1f}s) {losses}负 {timeouts}超时；"
                f"{'缓存' if cached else '模拟'}时间：{stamp}"
            ))],
        )
        result._training_snapshot = snapshot
        return result


def validate_response(item, attack, defend, completed_at=None):
    """Validate raw JSON before coercion, qualification or persistence."""
    if not isinstance(item, dict) or item.get("Success") is not True:
        raise RecommendationError("模拟响应：候选未成功完成")
    fields = [item.get(name) for name in ("ResultList", "BattleFramesList", "CurrentSeedList")]
    if any(not isinstance(values, list) or len(values) != REPEAT_COUNT for values in fields):
        raise RecommendationError("模拟协议：重复结果、帧数和种子必须各有10项")
    statuses, frames, seeds = fields
    if any(type(x) is not int or x not in (0, 1, 2) for x in statuses):
        raise RecommendationError("模拟协议：存在未知或非法胜负状态")
    if any(type(x) is not int or not 0 <= x <= 90 * 60 for x in frames):
        raise RecommendationError("模拟协议：帧数超出60fps、90秒契约")
    if any(type(x) is not int for x in seeds):
        raise RecommendationError("模拟协议：种子必须为整数")
    for name, expected in (("PlayerList", attack), ("EnemyList", defend)):
        if item.get(name):
            try:
                echoed = [unit["unitId"] for unit in item[name]]
            except (TypeError, KeyError):
                raise RecommendationError("模拟协议：返回队伍格式错误") from None
            if sorted(echoed) != sorted(expected):
                raise RecommendationError("模拟协议：返回队伍与请求不符")
    return Recommendation(tuple(attack), tuple(defend), tuple(zip(*fields)),
                          time.time() if completed_at is None else completed_at)


def combine_recommendations(groups, stop=None):
    durations = {id(record): tuple(frames / 60 for status, frames, _ in record.samples if status == 0)
                 for group in groups for record in group}
    legacy = {id(record): record.legacy_rank for group in groups for record in group}
    def transform(group):
        ids = [uid for record in group for uid in record.attack]
        return group if len(ids) == len(set(ids)) else None

    def key(group):
        wins = [value for record in group for value in durations[id(record)]]
        return (-len(wins) / (REPEAT_COUNT * len(group)), sum(wins) / len(wins),
                sum(legacy[id(record)] for record in group))

    return top_combinations(groups, key, 9, stop, transform)


class CounterattackService:
    def __init__(self, cache_path=None, model_dir=None, history_path=None,
                 endpoint_provider=None, identity_provider=None):
        self.cache_path = Path(cache_path or Path(CACHE_DIR) / "arena_mzq" / "recommendations-v3.sqlite3")
        self.model_dir = Path(model_dir or Path(DATA_DIR) / "jjc")
        self.history_path = Path(history_path or Path(CACHE_DIR) / "jjc_server" / "jjc_record.sqlite3")
        self.endpoint_provider = endpoint_provider or self._endpoint
        # A verified identity must identify BOTH engine and data. No URL heuristic.
        self.identity_provider = identity_provider or (lambda endpoint: None)
        self._cpu = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arena-mzq-model")
        self._db = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arena-mzq-db")
        self._cpu_gate = asyncio.Lock()
        self._db_gate = asyncio.Lock()
        self._http_gate = asyncio.Semaphore(3)
        self._build_locks = weakref.WeakValueDictionary()
        self._unknown_contexts = {}
        self._templates = {}
        self._template_lock = asyncio.Lock()
        self._process_id = uuid.uuid4().hex
        self._model = None
        self._model_digest = None

    @staticmethod
    def _endpoint():
        from ..mzq.mzq import instance
        return instance.simulate_url

    async def close(self):
        await asyncio.to_thread(self._cpu.shutdown, wait=True, cancel_futures=True)
        await asyncio.to_thread(self._db.shutdown, wait=True, cancel_futures=True)

    async def _work(self, executor, gate, state, function, *args, drain=False):
        await gate.acquire()
        owns_gate = True
        try:
            state.check()

            def run():
                state.check()
                return function(*args)

            future = asyncio.get_running_loop().run_in_executor(executor, run)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                state.stop.set()
                if drain:
                    while not future.done():
                        try:
                            await asyncio.shield(future)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if future.done() and not future.cancelled():
                        future.exception()
                else:
                    owns_gate = False

                    def finished(done):
                        if not done.cancelled():
                            done.exception()
                        gate.release()

                    future.add_done_callback(finished)
                raise
        finally:
            if owns_gate:
                gate.release()

    async def _cpu_work(self, state, function, *args):
        return await self._work(self._cpu, self._cpu_gate, state, function, *args)

    async def _db_work(self, state, function, *args):
        return await self._work(self._db, self._db_gate, state, function, *args, drain=True)

    def _bundle(self):
        manifest_bytes = (self.model_dir / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        hashes = {}
        for name, expected in manifest["files"].items():
            if name not in {"generator.onnx", "critic.onnx", "vocabulary.json"}:
                raise RecommendationError("模型校验：清单包含未知文件")
            actual = hashlib.sha256((self.model_dir / name).read_bytes()).hexdigest()
            if actual != expected:
                raise RecommendationError(f"模型校验：{name} SHA256不匹配")
            hashes[name] = actual
        if set(hashes) != {"generator.onnx", "critic.onnx", "vocabulary.json"}:
            raise RecommendationError("模型校验：模型包不完整")
        return hashlib.sha256(manifest_bytes + json.dumps(hashes, sort_keys=True).encode()).hexdigest()

    def _load_model(self, digest):
        from ..jjc_server.onnx_model_manager import JJCModelManager
        if self._model is None or digest != self._model_digest:
            self._model = JJCModelManager(self.model_dir / "generator.onnx", self.model_dir / "critic.onnx")
            self._model_digest = digest
        self._model.load()

    def _connect(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.cache_path), timeout=1)
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("""CREATE TABLE IF NOT EXISTS recommendations_v3 (
            context TEXT NOT NULL, defend TEXT NOT NULL, attack TEXT NOT NULL,
            completed_at REAL NOT NULL, payload TEXT NOT NULL, metadata TEXT NOT NULL,
            PRIMARY KEY(context, defend, attack))""")
        return conn

    def _read(self, context, defend, available, ttl):
        now = time.time()
        records = []
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT payload,completed_at,attack,metadata FROM recommendations_v3 WHERE context=? AND defend=? ORDER BY rowid",
                                (context, json.dumps(defend))).fetchall()
        for payload, timestamp, attack_key, metadata in rows:
            try:
                if not math.isfinite(timestamp) or not 0 <= now - timestamp <= ttl:
                    continue
                data = json.loads(payload)
                attack = tuple(data["attack"])
                if (tuple(json.loads(attack_key)) != attack
                        or hashlib.sha256(json.dumps(json.loads(metadata), sort_keys=True).encode()).hexdigest() != context
                        or any(not isinstance(sample, list) or len(sample) != 3 for sample in data['samples'])):
                    continue
                if tuple(data["defend"]) != tuple(defend) or not valid_team(attack, available):
                    continue
                item = dict(Success=True, ResultList=[v[0] for v in data["samples"]],
                            BattleFramesList=[v[1] for v in data["samples"]],
                            CurrentSeedList=[v[2] for v in data["samples"]])
                record = validate_response(item, attack, defend, timestamp)
                if record.wins >= MIN_WINS:
                    records.append(record)
            except (ValueError, TypeError, KeyError, IndexError, RecommendationError):
                log.warning("忽略损坏的回刺缓存记录")
        return sorted(records, key=lambda record: record.rank)

    def _write(self, state, context, metadata, records):
        with closing(self._connect()) as conn:
            try:
                state.check()
                conn.execute("BEGIN IMMEDIATE")
                for record in records:
                    state.check()
                    payload = dict(attack=record.attack, defend=record.defend, samples=record.samples)
                    conn.execute("""INSERT INTO recommendations_v3 VALUES (?,?,?,?,?,?)
                        ON CONFLICT(context,defend,attack) DO UPDATE SET
                        completed_at=excluded.completed_at,payload=excluded.payload,metadata=excluded.metadata""", (
                        context, json.dumps(record.defend), json.dumps(record.attack), record.completed_at,
                        json.dumps(payload), json.dumps(metadata, sort_keys=True),
                    ))
                state.check()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _history(self, defend, available):
        if not self.history_path.is_file():
            return [], []
        conn = sqlite3.connect(self.history_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            conn.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 1
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result_tables = [name for name in ("jjc_results", "jjc_results_buffer_b", "jjc_results_buffer_c") if name in tables]
            if "jjc_teams" not in tables or not result_tables:
                return [], []
            team_columns = ",".join(f"a.unit_{i}" for i in range(1, 6))
            # Inputs are integer IDs; literals avoid SQLite's parameter limit.
            in_list = ",".join(map(str, sorted(available))) or "NULL"
            owned = " AND ".join(f"a.unit_{i} IN ({in_list})" for i in range(1, 6))
            union = " UNION ALL ".join(f"SELECT * FROM {name}" for name in result_tables)
            def take_valid(cursor):
                result = []
                for row in cursor:
                    attack = tuple(row[:5])
                    if valid_team(attack, available):
                        result.append(attack)
                        if len(result) == 10:
                            break
                return result

            defend_match = " AND ".join(f"d.unit_{i}=?" for i in range(1, 6))
            exact = take_valid(conn.execute(f"""SELECT {team_columns},
                SUM(r.win_0_15+r.win_15_30+r.win_30_60+r.win_60_90) AS wins,
                SUM(r.win_0_15+r.win_15_30+r.win_30_60+r.win_60_90+r.losses+r.timeouts) AS runs
                FROM ({union}) r JOIN jjc_teams a ON a.team_id=r.attack_team_id
                JOIN jjc_teams d ON d.team_id=r.defend_team_id
                WHERE {owned} AND {defend_match} GROUP BY r.attack_team_id HAVING wins>0
                ORDER BY CAST(wins AS REAL)/runs DESC,wins DESC,runs DESC,r.attack_team_id""", defend))
            hot = take_valid(conn.execute(f"""SELECT {team_columns},
                SUM(r.win_0_15+r.win_15_30+r.win_30_60+r.win_60_90+r.losses+r.timeouts) AS runs
                FROM ({union}) r JOIN jjc_teams a ON a.team_id=r.attack_team_id
                WHERE {owned} AND r.last_battle_ts>=? GROUP BY r.attack_team_id
                ORDER BY runs DESC,r.attack_team_id""", (time.time() - 7 * 86400,)))
            return exact, hot
        except sqlite3.Error:
            log.warning("历史候选：只读读取失败，继续使用模型")
            return [], []
        finally:
            conn.close()

    def _candidates(self, state, digest, defend, available, history):
        state.check()
        self._load_model(digest)
        if not self._model.supports_team(defend):
            raise RecommendationError("角色支持：模型不支持该防守")
        roster = sorted(uid for uid in available if self._model.vocabulary.contains(uid))
        if len(roster) < 5:
            raise RecommendationError("角色不足：账号与模型词表交集不足5人")
        exact, hot = history
        generated = self._model.recall(defend, "fast_attack", roster=roster,
                                       candidate_count=256, generation_batch_size=64)
        state.check()
        exact = [team for team in exact if valid_team(team, available)][:10]
        other = list(dict.fromkeys(tuple(sorted(team)) for team in [*hot, *generated]
                                  if valid_team(team, available) and self._model.supports_team(team)))
        other = [team for team in other if team not in exact]
        if other:
            _, scores = self._model.rank(other, defend, "fast_attack")
            other = [other[index] for index in sorted(range(len(other)), key=lambda i: -float(scores[i]))][:54]
        state.check()
        return list(dict.fromkeys([*exact, *other]))[:64]

    async def _post(self, session, endpoint, state, body):
        async with self._http_gate:
            state.check()
            try:
                timeout = aiohttp.ClientTimeout(total=max(.001, state.deadline - time.monotonic()))
                async with session.post(endpoint, json=body, timeout=timeout) as response:
                    response.raise_for_status()
                    data = await response.json(content_type=None)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RecommendationError("模拟连接：HTTP、超时或响应解析失败") from None
        state.check()
        if not isinstance(data, list) or len(data) != len(body):
            raise RecommendationError("模拟协议：顶层响应数量与请求不一致")
        return data

    async def _warm_templates(self, session, endpoint, state, context, required, defend):
        """Only completed, validated templates are shared; each caller owns its HTTP work."""
        from ..jjc_server.simulation_config import build_battle_request
        async with self._template_lock:
            state.check()
            now = time.monotonic()
            # Also prune obsolete environments so endpoint/profile churn is bounded.
            self._templates = {key: value for key, value in self._templates.items()
                               if key[0] == context and now - value[0] < 300}
            missing = [uid for uid in sorted(required) if (context, uid) not in self._templates]
            requests = []
            for start in range(0, len(missing), 5):
                team = missing[start:start + 5]
                team += [uid for uid in sorted(required) if uid not in team][:5 - len(team)]
                request = build_battle_request(team, defend, 1)
                # RepeatCount>1 is cloned through Json.NET, which supplies these
                # constructor defaults. Warmup uses one run: initialize explicitly
                # so its auto-EX result has the same structure as normal repeats.
                for unit in request['PlayerList'] + request['EnemyList']:
                    unit['exEquipSubs'] = {str(slot): [0] * 8 for slot in range(3)}
                requests.append(request)
            started = time.monotonic()
            obtained = {}
            try:
                for start in range(0, len(requests), 64):
                    batch = requests[start:start + 64]
                    data = await self._post(session, endpoint, state, batch)
                    for request, item in zip(batch, data):
                        if not isinstance(item, dict) or item.get('Success') is not True:
                            raise TrainingError('满练预热：候选未成功完成')
                        for field in ('PlayerList', 'EnemyList'):
                            rows = item.get(field)
                            expected = {row['unitId'] for row in request[field]}
                            if (not isinstance(rows, list) or len(rows) != 5
                                    or any(not isinstance(row, dict) for row in rows)
                                    or {row.get('unitId') for row in rows} != expected):
                                raise TrainingError('满练预热：返回角色ID或数量不一致')
                            for row in rows:
                                template = validate_template(row)
                                uid = template['unitId']
                                if uid in obtained and obtained[uid] != template:
                                    raise TrainingError('满练预热：同一角色返回不一致的模板')
                                obtained[uid] = template
                state.check()
                stamp = time.monotonic()
                self._templates.update({(context, uid): (stamp, template) for uid, template in obtained.items()})
            except BaseException:
                self._templates.clear()
                raise
            log.info('回刺满练模板预热：%d项，%d个HTTP批次，%.3f秒', len(requests),
                     (len(requests) + 63) // 64, time.monotonic() - started)
            return {uid: json.loads(json.dumps(self._templates[(context, uid)][1])) for uid in required}

    @staticmethod
    def _battle_request(attack, defend, snapshot, templates, units=None):
        from ..jjc_server.simulation_config import build_battle_request
        request = build_battle_request(attack, defend, REPEAT_COUNT)
        request['ToMaxUnitAttr'] = False
        request['PlayerList'] = [actual_player(snapshot, uid, templates[uid], units) for uid in attack]
        request['EnemyList'] = [json.loads(json.dumps(templates[uid])) for uid in defend]
        for field, value in snapshot.progression.items():
            request['Enemy' + field] = request[field]
            request[field] = value
        return request

    async def _simulate(self, session, endpoint, state, candidates, defend, snapshot, templates):
        def build():
            units = snapshot.units
            return [self._battle_request(attack, defend, snapshot, templates, units) for attack in candidates]
        body = await self._cpu_work(state, build)
        data = await self._post(session, endpoint, state, body)
        records, errors = [], []
        for attack, request, item in zip(candidates, body, data):
            try:
                record = validate_response(item, attack, defend)
                assert_formal_units(item, request['PlayerList'], request['EnemyList'])
                if record.wins >= MIN_WINS:
                    records.append(record)
            except (RecommendationError, TrainingError) as error:
                errors.append(str(error))
        return records, errors

    async def query(self, available, defends, snapshot=None):
        if not isinstance(snapshot, TrainingSnapshot):
            raise RecommendationError('账号练度：缺少本次查询的不可变练度快照')
        available = frozenset(available)
        if any(type(uid) is not int or uid <= 0 for uid in available):
            raise RecommendationError("角色输入：角色池包含非法ID")
        available = available.intersection(snapshot.units)
        if len(available) < 5:
            raise RecommendationError('账号练度：可用角色不足5人')
        defends = [tuple(sorted(defend)) for defend in defends]
        if not 1 <= len(defends) <= 3 or any(not valid_team(d, set(d)) for d in defends):
            raise RecommendationError("防守输入：需要完整的5人不同角色队伍")
        seconds = configured_seconds("AUTOPCR_ARENA_MZQ_TIMEOUT", 120)
        state = RequestState(time.monotonic() + seconds, Event())
        collected = [[] for _ in defends]
        cached_attacks = [set() for _ in defends]
        outcome = {'ttl': 300}
        endpoint = str(self.endpoint_provider())
        errors = []
        groups = None
        try:
            await asyncio.wait_for(self._query(state, endpoint, available, defends, collected,
                                             cached_attacks, errors, outcome, snapshot), seconds)
            # Successful queries include combination work in the mzq budget.
            if len(defends) > 1:
                remaining = state.deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                groups = await asyncio.wait_for(self._cpu_work(
                    state, combine_recommendations, [group[:64] for group in collected], state.stop), remaining)
        except asyncio.CancelledError:
            state.stop.set()
            raise
        except asyncio.TimeoutError:
            errors.append("超时：mzq阶段预算已耗尽")
        except (RecommendationError, TrainingError) as error:
            errors.append(str(error))
        except (OSError, ValueError, KeyError, sqlite3.Error):
            errors.append("模型或缓存：加载、校验或数据库访问失败")
            log.exception("回刺推荐阶段失败")
        finally:
            state.stop.set()
        if errors:
            log.warning("回刺mzq：%s", "; ".join(dict.fromkeys(errors)))
        # A cache row may have expired while refresh was running.
        for index, records in enumerate(collected):
            fresh_enough = [record for record in records
                            if 0 <= time.time() - record.completed_at <= outcome['ttl']]
            if len(fresh_enough) != len(records):
                groups = None
            collected[index][:] = fresh_enough
        if groups is None:
            # Bounded recovery from already validated partial results. Cooperative
            # cancellation also applies while presenting these surviving results.
            stop = Event()
            future = asyncio.create_task(asyncio.to_thread(
                combine_recommendations, [group[:64] for group in collected], stop)) if len(defends) > 1 else None
            try:
                groups = ([[record] for record in sorted(collected[0], key=lambda r: r.rank)]
                          if future is None else await asyncio.shield(future))
            finally:
                stop.set()
                if future is not None and not future.done():
                    await asyncio.shield(future)
        if not groups:
            raise RecommendationError("；".join(dict.fromkeys(errors)) if errors else "无解：没有完整的不重复可用方案")
        result = [[record.result(snapshot, record.attack in cached_attacks[index]) for index, record in enumerate(group)]
                  for group in groups]
        if errors:
            result[0][0].comment[0].msg += '；刷新提示：' + '；'.join(dict.fromkeys(errors))
        return result

    async def _query(self, state, endpoint, available, defends, collected, cached_attacks, errors, outcome, snapshot):
        from ..jjc_server.simulation_config import build_battle_request
        digest = await self._cpu_work(state, self._bundle)
        params = build_battle_request([], [], REPEAT_COUNT)
        identity = self.identity_provider(endpoint)
        unknown_key = (endpoint, digest, json.dumps(params, sort_keys=True))
        if getattr(self, '_last_environment', None) != unknown_key:
            self._unknown_contexts.clear()
            self._templates.clear()
            self._last_environment = unknown_key
        session_id = self._unknown_contexts.setdefault(unknown_key, uuid.uuid4().hex)
        metadata = dict(model=digest, endpoint_digest=hashlib.sha256(endpoint.encode()).hexdigest(),
                        parameters=self._battle_request([], [], snapshot, {}), validation=VALIDATION_VERSION,
                        mode='max-training-actual-rank-progression-auto-five-star', training=snapshot.fingerprint,
                        engine_data=identity, process=None if identity else self._process_id,
                        session=None if identity else session_id)
        template_context = training_digest([endpoint, identity, digest, params,
                                            None if identity else session_id])
        ttl = 15 * 86400 if identity else 300
        outcome['ttl'] = ttl

        async def defend_query(index, defend, session):
            lock_key = (context, defend, tuple(sorted(available)))
            lock = self._build_locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                records = await self._db_work(state, self._read, context, defend, available, ttl)
                collected[index].extend(records)
                cached_attacks[index].update(record.attack for record in records)
                if len(records) >= 16:
                    return
                history = await self._db_work(state, self._history, defend, available)
                try:
                    candidates = await self._cpu_work(state, self._candidates, state, digest, defend, available, history)
                except RecommendationError:
                    raise
                except Exception as error:
                    log.warning("回刺模型推理失败：%s", type(error).__name__)
                    raise RecommendationError("模型推理：加载或候选生成失败") from None
                candidates = [team for team in candidates if valid_team(team, available)]
                if not candidates:
                    return
                fresh, invalid = await self._simulate(session, endpoint, state, candidates, defend, snapshot, templates)
                errors.extend(invalid)
                if invalid and not identity:
                    self._unknown_contexts.pop(unknown_key, None)
                    self._templates.clear()
                merged = {record.attack: record for record in records}
                merged.update((record.attack, record) for record in fresh)
                collected[index][:] = sorted(merged.values(), key=lambda record: record.rank)
                cached_attacks[index].difference_update(record.attack for record in fresh)
                if fresh:
                    await self._db_work(state, self._write, state, context, metadata, fresh)

        async with aiohttp.ClientSession() as session:
            try:
                templates = await self._warm_templates(session, endpoint, state, template_context,
                    set(available).union(*defends), defends[0])
            except BaseException:
                if not identity:
                    self._unknown_contexts.pop(unknown_key, None)
                raise
            metadata['templates'] = training_digest(templates)
            context = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()
            tasks = [asyncio.create_task(defend_query(index, defend, session)) for index, defend in enumerate(defends)]
            try:
                await asyncio.gather(*tasks)
            except BaseException as error:
                if (not identity and isinstance(error, RecommendationError)
                        and str(error).startswith(('模拟连接', '模拟协议'))):
                    self._unknown_contexts.pop(unknown_key, None)
                    self._templates.clear()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise


_services = weakref.WeakKeyDictionary()


def get_service():
    loop = asyncio.get_running_loop()
    if loop not in _services:
        _services[loop] = CounterattackService()
    return _services[loop]
