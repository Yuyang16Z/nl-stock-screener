"""监控调度（DESIGN §7.2）：每 10 分钟检查到期监控（启动后补跑过期任务）；每次运行与上次成功运行做 diff。

数据集 partial/failed 时写 data_alerts，且此时绝不把结果差异描述为“无变化”。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any

from ..data.periods import CN_TZ, now_cn
from ..engine.diff import diff_results
from ..schemas import Monitor, MonitorRun, ResultDiff, ScreenRequest, ScreenResult
from .runs import NotFoundError, RunManager
from .store import Store

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 600.0
STARTUP_DELAY_S = 30.0
RUN_TIMEOUT_S = 1800.0
DAILY_RUN_AT = time(16, 30)
ALERT_CAVEAT = "本次有数据集部分失败或失败，入选结果可能不完整：未出现新增/移除不代表实际没有变化。"


def next_run_at(frequency: str, now: datetime | None = None) -> datetime:
    """daily = 次日 16:30（北京时间）；weekly = 7 天后的 16:30。"""
    base = (now or now_cn()).astimezone(CN_TZ)
    days = 7 if frequency == "weekly" else 1
    day = base.date() + timedelta(days=days)
    return datetime.combine(day, DAILY_RUN_AT, tzinfo=CN_TZ)


def data_alerts(result: ScreenResult) -> list[str]:
    alerts: list[str] = []
    for ds in result.datasets:
        if ds.status in ("partial", "failed"):
            what = "部分失败" if ds.status == "partial" else "失败"
            stat = f"（成功 {ds.succeeded}/{ds.requested}）" if ds.requested else ""
            sample = f"；错误样例：{ds.errors[0]}" if ds.errors else ""
            alerts.append(f"{ds.label} 数据{what}{stat}，相关股票按数据缺失处理{sample}")
    return alerts


class MonitorScheduler:
    def __init__(self, store: Store, runs: RunManager, settings: Any | None = None) -> None:
        self.store = store
        self.runs = runs
        self.settings = settings
        self._loop_task: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inflight: dict[str, str] = {}  # monitor_id -> run_id

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, *self._tasks] if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:  # noqa: BLE001
                pass
        self._loop_task = None

    async def _loop(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_S)
        while True:
            try:
                await self.check_due()
            except Exception:  # noqa: BLE001
                log.exception("监控调度检查失败")
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def check_due(self, now: datetime | None = None) -> list[str]:
        now = now or now_cn()
        started: list[str] = []
        for m in self.store.list_monitors():
            if not m.enabled or m.id in self._inflight:
                continue
            if m.next_run_at is None or m.next_run_at <= now:
                try:
                    started.append(self.run_now(m.id))
                except Exception:  # noqa: BLE001
                    log.exception("监控 %s 启动失败", m.id)
        return started

    def run_now(self, monitor_id: str) -> str:
        m = self.store.get_monitor(monitor_id)
        if m is None:
            raise NotFoundError("监控任务不存在")
        run_id = self.runs.start(ScreenRequest(strategy=m.strategy))
        now = now_cn()
        m.runs.append(MonitorRun(run_at=now, run_id=run_id, status="running"))
        m.last_run_at = now
        m.next_run_at = next_run_at(m.frequency, now)
        self.store.save_monitor(m)
        self._inflight[monitor_id] = run_id
        task = asyncio.create_task(self._finish(monitor_id, run_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _finish(self, monitor_id: str, run_id: str) -> None:
        try:
            try:
                result = await self.runs.wait(run_id, RUN_TIMEOUT_S)
            except asyncio.TimeoutError:
                result = None
            m = self.store.get_monitor(monitor_id)
            if m is None:
                return
            entry = next((r for r in m.runs if r.run_id == run_id), None)
            if entry is None:
                entry = MonitorRun(run_at=now_cn(), run_id=run_id, status="running")
                m.runs.append(entry)
            if result is None:
                entry.status, entry.error = "failed", "运行超时（超过 30 分钟）"
            elif result.status != "done":
                entry.status, entry.error = "failed", result.error or "运行失败"
                entry.data_alerts = data_alerts(result)
            else:
                entry.status = "done"
                entry.included_count = len(result.included)
                entry.trade_date = result.trade_date
                entry.data_alerts = data_alerts(result)
                entry.diff = self._diff(m, run_id, entry.data_alerts)
            self.store.save_monitor(m)
        except Exception:  # noqa: BLE001
            log.exception("监控运行收尾失败 monitor=%s run=%s", monitor_id, run_id)
        finally:
            if self._inflight.get(monitor_id) == run_id:
                self._inflight.pop(monitor_id, None)

    def _diff(self, m: Monitor, run_id: str, alerts: list[str]) -> ResultDiff | None:
        prev = None
        for r in reversed(m.runs):
            if r.run_id and r.run_id != run_id and r.status == "done":
                prev = r
                break
        if prev is None or prev.run_id is None:
            return None
        try:
            base = self.runs.record(prev.run_id)
            cur = self.runs.record(run_id)
        except NotFoundError:
            return None
        if base.evaluation is None or cur.evaluation is None:
            return None
        diff = diff_results(
            base.evaluation, cur.evaluation, base.result.strategy, cur.result.strategy,
            base_run_id=prev.run_id, new_run_id=run_id,
        )
        if alerts:
            diff.changes_text = [ALERT_CAVEAT, *[t for t in diff.changes_text if t != "策略未变化"]]
        return diff
