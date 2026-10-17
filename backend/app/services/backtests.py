"""回测任务编排：按 engine/backtest.py 模块文档组装 BacktestData，在线程中执行 run_backtest。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from typing import Any

from ..config import Settings
from ..data.features import Fetched, parse_bars
from ..data.fuyao import FuyaoError
from ..data.periods import history_window, now_cn, today_cn
from ..data.repository import skipped_status, status_from_fetched
from ..engine.backtest import INCOME_METRICS, BacktestData, StockSeries, run_backtest
from ..engine.conflicts import validate_strategy
from ..schemas import BacktestRequest, BacktestResult, DatasetStatus, StockInfo
from .runs import NotFoundError, RunError, error_text

log = logging.getLogger(__name__)

MAX_BACKTESTS = 30


class BacktestManager:
    def __init__(self, settings: Settings, repository: Any) -> None:
        self.settings = settings
        self.repo = repository
        self._items: OrderedDict[str, BacktestResult] = OrderedDict()
        self._tasks: set[asyncio.Task[Any]] = set()

    def get(self, backtest_id: str) -> BacktestResult:
        res = self._items.get(backtest_id)
        if res is None:
            raise NotFoundError("回测任务不存在（可能已过期或后端已重启），请重新发起回测")
        return res

    def start(self, req: BacktestRequest) -> str:
        bid = uuid.uuid4().hex[:12]
        self._items[bid] = BacktestResult(id=bid, status="pending", progress_text="排队中", request=req, created_at=now_cn())
        while len(self._items) > MAX_BACKTESTS:
            self._items.popitem(last=False)
        task = asyncio.create_task(self._execute(bid, req))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return bid

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except BaseException:  # noqa: BLE001
                pass

    def _set(self, bid: str, **kw: Any) -> None:
        cur = self._items.get(bid)
        if cur is not None:
            self._items[bid] = cur.model_copy(update=kw)

    async def _execute(self, bid: str, req: BacktestRequest) -> None:
        created = self._items[bid].created_at
        try:
            self._set(bid, status="running", progress=0.01, progress_text="校验条件")
            vr = validate_strategy(req.strategy)
            errors = [c for c in vr.conflicts if c.severity == "error"]
            if errors:
                raise RunError("条件存在冲突，无法回测：" + "；".join(c.message for c in errors))
            req = req.model_copy(update={"strategy": vr.strategy})
            client = getattr(self.repo, "client", None)
            if client is not None and not getattr(client, "enabled", True):
                raise RunError("未配置扶摇 API Key（FUYAO_API_KEY），无法获取历史数据进行回测。")
            data = await self._assemble(bid, req)
            self._set(bid, progress=0.5, progress_text="开始逐期判定")

            def progress(frac: float, text: str) -> None:
                self._set(bid, progress=round(0.5 + 0.5 * max(0.0, min(1.0, frac)), 4), progress_text=text)

            result = await asyncio.to_thread(run_backtest, req, data, progress, backtest_id=bid)
            self._items[bid] = result.model_copy(update={"created_at": created, "finished_at": now_cn(), "request": req})
        except asyncio.CancelledError:
            self._set(bid, status="failed", error="回测被取消（服务正在关闭）", finished_at=now_cn())
            raise
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, (RunError, FuyaoError, ValueError)):
                log.exception("回测异常 id=%s", bid)
            self._set(bid, status="failed", error=error_text(e), progress_text="回测失败", finished_at=now_cn())

    async def _assemble(self, bid: str, req: BacktestRequest) -> BacktestData:
        strategy = req.strategy
        self._set(bid, progress=0.03, progress_text="解析股票池")
        universe = await self.repo.resolve_universe(strategy.universe)
        members = list(universe.members)
        limit = int(self.settings.max_deep_eval)
        if not members:
            raise RunError(f"股票池“{universe.name}”没有可用成分股")
        if len(members) > limit:
            raise RunError(f"回测股票池共 {len(members)} 只，超过深度评估上限 {limit}，请换更小的股票池（如沪深300、中证500）")
        metrics = {c.metric for c in strategy.conditions if c.enabled}
        need_raw = "pe_ttm" in metrics
        need_income = bool(metrics & set(INCOME_METRICS))
        start_ms, end_ms = history_window(today_cn())
        statuses: dict[str, DatasetStatus] = {}

        # 基准：交易日历与基准收盘
        self._set(bid, progress=0.06, progress_text=f"取基准指数日K（{req.benchmark}）")
        try:
            bresp = await self.repo.index_history(req.benchmark, start_ms, end_ms)
        except FuyaoError as e:
            raise RunError(f"基准指数日K获取失败，无法确定交易日历：{error_text(e)}") from e
        bench_bars = [b for b in parse_bars(bresp.data) if b.close is not None]
        trade_dates = [b.day.isoformat() for b in bench_bars]
        benchmark_close = {b.day.isoformat(): float(b.close) for b in bench_bars if b.close is not None}
        statuses["benchmark"] = status_from_fetched("benchmark", [req.benchmark], {req.benchmark: Fetched("benchmark", response=bresp)})

        # 代码表
        info: dict[str, StockInfo] = {}
        try:
            table = await self.repo.ticker_table()
            for code in members:
                row = table.get(code)
                if row is not None:
                    info[code] = StockInfo(thscode=code, ticker=row.ticker, name=row.name, list_date=row.list_date)
            statuses["tickers"] = DatasetStatus(dataset="tickers", label="A股代码表", endpoint="/api/meta/tickers/list",
                                                status="ok", requested=1, succeeded=1)
        except FuyaoError as e:
            statuses["tickers"] = DatasetStatus(dataset="tickers", label="A股代码表", endpoint="/api/meta/tickers/list",
                                                status="failed", requested=1, failed=1, errors=[e.brief()])

        total = len(members) * (1 + int(need_raw) + int(need_income))
        done = [0]

        def tick(text: str) -> None:
            done[0] += 1
            self._set(bid, progress=round(0.08 + 0.42 * done[0] / max(1, total), 4), progress_text=f"{text} {done[0]}/{total}")

        async def capture(aw: Any) -> Any:
            try:
                return await aw
            except FuyaoError as e:
                return e

        hist_f: dict[str, Fetched] = {}
        raw_f: dict[str, Fetched] = {}
        inc_f: dict[str, Fetched] = {}

        async def one(code: str) -> None:
            out = await capture(self.repo.history(code, start_ms, end_ms, "backward"))
            hist_f[code] = Fetched("history", error=out) if isinstance(out, FuyaoError) else Fetched("history", response=out)
            tick("取个股日K")
            if need_raw:
                out = await capture(self.repo.history(code, start_ms, end_ms, "none"))
                raw_f[code] = Fetched("history", error=out) if isinstance(out, FuyaoError) else Fetched("history", response=out)
                tick("取不复权日K")
            if need_income:
                out = await capture(self.repo.income_quarterly(code, 20))
                inc_f[code] = Fetched("income", error=out) if isinstance(out, FuyaoError) else Fetched("income", response=out)
                tick("取利润表")

        await asyncio.gather(*(one(c) for c in members))

        prices: dict[str, StockSeries] = {}
        for code in members:
            f = hist_f.get(code)
            if f is None or not f.ok or f.response is None:
                continue
            bars = parse_bars(f.response.data)
            if not bars:
                continue
            dates = [b.day.isoformat() for b in bars]
            close_raw = None
            rf = raw_f.get(code)
            if need_raw and rf is not None and rf.ok and rf.response is not None:
                raw_map = {b.day.isoformat(): b.close for b in parse_bars(rf.response.data)}
                close_raw = [raw_map.get(d) for d in dates]
            prices[code] = StockSeries(dates=dates, close=[b.close for b in bars], close_raw=close_raw,
                                       turnover=[b.turnover for b in bars])
        income: dict[str, list[dict]] = {}
        for code, f in inc_f.items():
            if f.ok and f.response is not None:
                income[code] = list(f.response.items)

        statuses["history"] = status_from_fetched("history", members, hist_f, "后复权日K（回测）" + ("；另取不复权日K用于重建 PE" if need_raw else ""))
        statuses["income"] = status_from_fetched("income", members, inc_f) if need_income else skipped_status("income")
        statuses["constituents"] = DatasetStatus(dataset="constituents", label="指数/板块成分股", endpoint=universe.source.endpoint if universe.source and universe.source.endpoint else "",
                                                 status="ok", requested=1, succeeded=1, note=universe.note or "成分股为当前时点")
        order = ["benchmark", "history", "income", "tickers", "constituents"]
        return BacktestData(
            trade_dates=trade_dates, benchmark_close=benchmark_close, prices=prices, income=income, info=info,
            members=members, datasets=[statuses[k] for k in order if k in statuses],
        )
