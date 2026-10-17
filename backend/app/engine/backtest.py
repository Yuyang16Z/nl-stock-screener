"""回测引擎（DESIGN §5.5）：纯计算、不取数，严格时点一致（point-in-time）。

一、时点与成交规则
- 调仓日 D 判定条件只用：D 前一交易日（含）及以前的后复权收盘 / 成交额 / 基准收盘，以及披露日早于 D 的利润表季报。
- 以 D 日收盘价（后复权）成交：只有日线收盘价可用；判定数据截止 D−1、成交在 D，无前视。
- 净值每个交易日一点：调仓日的点为调仓前持仓市值，新持仓与交易成本自下一交易日起体现；
  因此首点为初始资金 1.0（建仓前），基准同日归一为 1.0。
- 交易成本：cost_bps 为“每边”费率，成本 = (买入比例 + 卖出比例) × cost_bps ÷ 10000，在调仓日收盘从净值扣除。
- 区间内首个交易日建仓，其后每月 / 每季首个交易日调仓；区间最后一个交易日不调仓（之后已无持有期）。

二、services 层如何从扶摇响应组装 BacktestData（services/backtests.py 按此实现）
记 L = req.start 往前约 420 个自然日（覆盖 250 交易日窗口），E = req.end 或今天；毫秒戳与日期按 Asia/Shanghai 互转。
1. trade_dates、benchmark_close ← 基准指数日K（一次请求）
   GET /api/a-share-index/prices/historical?thscode={req.benchmark}&interval=1d&start=ms(L)&end=ms(E)
   （该接口只支持近 5 年）。每条 item：benchmark_close[ms_to_date(date_ms)] = close_price；
   trade_dates = 全部日期（yyyy-MM-dd）升序去重，作为交易日历；个股日K中落在日历区间内却不在日历里的日期会被忽略并计数。
2. prices[thscode] = StockSeries ← 个股日K（每只一次请求）
   - 后复权：GET /api/a-share/prices/historical?thscode=..&interval=1d&start=ms(L)&end=ms(E)&adjust=backward
     dates = 各 item 日期升序；close = close_price；turnover = turnover（元）；null 原样保留为 None。
     turnover == 0 的记录视为停牌（不参与窗口计算、估值沿用前值），与 features.trading_bars 剔除零成交一致。
   - 不复权（仅当启用条件含 pe_ttm 时需要）：同接口 adjust=none，按日期对齐到 dates 得到等长的 close_raw，
     对不上的位置填 None；不取时 close_raw=None（此时 pe_ttm 条件对所有股票按缺失处理，不入选）。
   - 请求失败的股票不放入 prices：引擎按“无价格”处理（价格类条件缺失、不可成交、不会入选）。
3. income[thscode] = 利润表季报 item 原样列表（dict）← GET /api/a-share/financials/income-statements
   ?thscode=..&period=quarterly&start=ms(req.start − 2 年)&end=ms(E)（时间区间模式，跨度 ≤ 10 年；或 limit=20）。
   需要字段：fiscal_year, fiscal_period, report_date_ms, period_end_ms, operating_income, operating_costs,
   operating_profit, net_profit, parent_holder_net_profit, basic_eps。仅当启用条件含成长 / 毛利率 / pe_ttm 时需要；
   请求失败的股票不放入（该股相关条件按缺失处理）。
4. info[thscode] = StockInfo ← 代码表 /api/meta/tickers/list（name 用于 is_st，list_date 用于 listing_days）。
5. members ← Repository.resolve_universe(strategy.universe).members（当前成分股，幸存者偏差已写入 caveats）。
6. datasets ← 上述数据集（benchmark / history / income / tickers / constituents）的 DatasetStatus，原样透传到结果；
   partial / failed 的数据集会写入 assumptions，受影响股票的条件按缺失处理（绝不视为通过）。
7. run_backtest 是同步 CPU 计算（800 只 × 36 次调仓约 2 秒），services 层应放到线程中执行（asyncio.to_thread），
   progress 回调会在该线程中被调用；返回结果的 created_at / finished_at 可由 services 用任务自身时间覆盖。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import numpy as np

from ..catalog import METRICS, OPS, RANK_OPS, RANK_SCOPE_LABEL
from ..schemas import (
    BacktestRequest,
    BacktestResult,
    Condition,
    CurvePoint,
    DatasetStatus,
    IgnoredCondition,
    MetricDef,
    MetricValue,
    PerfStats,
    RebalanceRecord,
    StockInfo,
    Strategy,
)
from .ops import MIN_RANK_GROUP, check_op, is_finite_number, pct_rank

__all__ = [
    "BacktestData",
    "StockSeries",
    "SUPPORTED",
    "run_backtest",
    "rebalance_dates",
    "income_metrics_as_of",
    "annualized_volatility",
    "max_drawdown_pct",
    "period_return_pct",
    "beta_coefficient",
    "perf_stats",
]

CN_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
TRADING_DAYS_PER_YEAR = 252
MIN_SAMPLE_RATIO = 0.8  # 窗口内有效样本 < N × 0.8 → 缺失
STALE_REPORT_DAYS = 400  # 最新已披露报告期末距调仓日超过该天数 → 视为过旧
SCREEN_BETA_BENCHMARK = "000300.SH"  # 筛选口径的 Beta 基准

ProgressFn = Callable[[float, str], None]
_MStatus = Literal["ok", "missing", "not_applicable"]
_CState = Literal["pass", "fail", "missing", "not_applicable"]

# 价格窗口类指标：metric → (计算方式, 窗口交易日数 N)
PRICE_WINDOWS: dict[str, tuple[str, int]] = {
    "volatility_250d": ("vol", 250),
    "max_drawdown_250d": ("mdd", 250),
    "beta_250d": ("beta", 250),
    "return_20d": ("ret", 20),
    "return_60d": ("ret", 60),
    "return_250d": ("ret", 250),
    "avg_amount_20d": ("amount", 20),
}
# 同比指标 → 利润表字段（净利润同比用 net_profit，与指标名“净利润”一致）
YOY_FIELDS: dict[str, str] = {
    "revenue_yoy": "operating_income",
    "net_profit_yoy": "net_profit",
    "op_profit_yoy": "operating_profit",
}
INCOME_METRICS: frozenset[str] = frozenset({*YOY_FIELDS, "gross_margin", "gross_margin_change", "pe_ttm"})
STATIC_METRICS: frozenset[str] = frozenset({"listing_days", "is_st"})
SUPPORTED: frozenset[str] = frozenset(PRICE_WINDOWS) | INCOME_METRICS | STATIC_METRICS

_INCOME_FIELDS = (
    "operating_income",
    "operating_costs",
    "operating_profit",
    "net_profit",
    "parent_holder_net_profit",
    "basic_eps",
)
_FIELD_LABEL = {
    "operating_income": "营业收入",
    "operating_costs": "营业成本",
    "operating_profit": "营业利润",
    "net_profit": "净利润",
    "parent_holder_net_profit": "归母净利润",
    "basic_eps": "基本每股收益",
}
_QUARTER_LABEL = {1: "一季报", 2: "中报", 3: "三季报", 4: "年报"}
_PERIOD_END_MD = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_DEADLINE = {1: (0, 4, 30), 2: (0, 8, 31), 3: (0, 10, 31), 4: (1, 4, 30)}  # (年偏移, 月, 日)：法定披露截止日
_OP_SYMBOL = {o.op: o.symbol for o in OPS}
_NUMERIC_OPS = {"gt", "gte", "lt", "lte", "between"}

_NOT_BACKTESTABLE: dict[str, str] = {
    "pb_mrq": "历史估值快照不可得；回测只支持由不复权价格与每股收益近似重建的市盈率TTM。",
    "ps_ttm": "历史估值快照不可得；回测只支持由不复权价格与每股收益近似重建的市盈率TTM。",
    "pcf_ttm": "历史估值快照不可得；回测只支持由不复权价格与每股收益近似重建的市盈率TTM。",
    "pct_change_today": "实时行情指标，没有历史时点含义。",
    "market_cap_est": "估算市值依赖实时估值快照，无法按历史时点重建。",
    "dividend_yield_ttm": "回测暂未重建历史时点的股息率。",
}


# ───────────────────────────── 输入数据结构 ─────────────────────────────


@dataclass
class StockSeries:
    """单只股票日K，各列表与 dates 等长对齐；元素为 None 表示该日该字段缺失。"""

    dates: list[str]  # yyyy-MM-dd 升序
    close: list[float | None]  # 后复权收盘
    close_raw: list[float | None] | None = None  # 不复权收盘；None = 未获取
    turnover: list[float | None] | None = None  # 成交额（元）；None = 未获取

    def __post_init__(self) -> None:
        n = len(self.dates)
        for name in ("close", "close_raw", "turnover"):
            seq = getattr(self, name)
            if seq is not None and len(seq) != n:
                raise ValueError(f"StockSeries.{name} 长度 {len(seq)} 与 dates 长度 {n} 不一致")


@dataclass
class BacktestData:
    """回测所需的全部原始数据（组装方式见模块文档）。"""

    trade_dates: list[str]  # yyyy-MM-dd 升序，来自基准指数日K
    benchmark_close: dict[str, float]  # date → 基准收盘
    prices: dict[str, StockSeries]  # thscode → 日K
    income: dict[str, list[dict]]  # thscode → 利润表季报原始 item
    info: dict[str, StockInfo]  # thscode → 代码表信息（当前名称、上市日期）
    members: list[str]  # 股票池（当前成分股）
    datasets: list[DatasetStatus] = field(default_factory=list)


# ───────────────────────────── 纯计算工具（可单测、可复用） ─────────────────────────────


def annualized_volatility(closes: Sequence[float] | np.ndarray) -> float | None:
    """年化波动率（%）= std(ln(Pt/Pt−1), ddof=1) × √252 × 100；closes 为按日期升序的有效正价格。"""
    p = np.asarray(closes, dtype=float)
    if p.size < 3:
        return None
    r = np.diff(np.log(p))
    return float(np.std(r, ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0


def max_drawdown_pct(closes: Sequence[float] | np.ndarray) -> float | None:
    """最大回撤（%，正数）= max(1 − Pt / max(Ps, s≤t)) × 100。"""
    p = np.asarray(closes, dtype=float)
    if p.size < 2:
        return None
    peak = np.maximum.accumulate(p)
    return float(np.max(1.0 - p / peak)) * 100.0


def period_return_pct(closes: Sequence[float] | np.ndarray) -> float | None:
    """区间涨跌幅（%）=（末价 ÷ 首价 − 1）× 100。"""
    p = np.asarray(closes, dtype=float)
    if p.size < 2:
        return None
    return (float(p[-1]) / float(p[0]) - 1.0) * 100.0


def beta_coefficient(stock_returns: Sequence[float] | np.ndarray, bench_returns: Sequence[float] | np.ndarray) -> float | None:
    """Beta = cov(ri, rm) ÷ var(rm)（同日配对的简单收益，ddof=1）；样本 < 3 或基准方差为 0 → None。"""
    s = np.asarray(stock_returns, dtype=float)
    b = np.asarray(bench_returns, dtype=float)
    if s.size != b.size or s.size < 3:
        return None
    var = float(np.var(b, ddof=1))
    if not var > 1e-18:
        return None
    cov = float(np.sum((s - s.mean()) * (b - b.mean())) / (s.size - 1))
    return cov / var


def perf_stats(values: Sequence[float] | np.ndarray) -> PerfStats:
    """净值序列 → 总收益、年化（252 日复利）、年化波动、最大回撤、夏普（无风险利率 0）；均为百分数值（夏普除外）。"""
    v = np.asarray(values, dtype=float)
    if v.size == 0 or not np.all(np.isfinite(v)) or v[0] <= 0 or np.any(v <= 0):
        return PerfStats()
    n = v.size - 1
    growth = float(v[-1] / v[0])
    annual: float | None = None
    if n > 0:
        try:
            annual = (growth ** (TRADING_DAYS_PER_YEAR / n) - 1.0) * 100.0
        except OverflowError:  # 极短区间的极端增长：年化无意义
            annual = None
    vol = sharpe = None
    if n >= 2:
        r = v[1:] / v[:-1] - 1.0
        sd = float(np.std(r, ddof=1))
        vol = sd * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0
        if sd > 1e-12:
            sharpe = float(np.mean(r)) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)
    mdd = float(np.max(1.0 - v / np.maximum.accumulate(v))) * 100.0
    return PerfStats(
        total_return=_r4((growth - 1.0) * 100.0),
        annual_return=_r4(annual),
        annual_vol=_r4(vol),
        max_drawdown=_r4(mdd),
        sharpe=_r4(sharpe),
    )


def rebalance_dates(trade_dates: Sequence[str], start: str, end: str | None, rebalance: str) -> list[str]:
    """[start, end] 内的调仓日：首个交易日建仓，其后每月/每季首个交易日；区间最后一个交易日不调仓。"""
    cal = sorted(set(trade_dates))
    i0 = bisect_left(cal, start)
    i1 = (bisect_right(cal, end) - 1) if end else len(cal) - 1
    return [cal[t] for t in _rebalance_indices(cal, i0, i1, rebalance)]


def income_metrics_as_of(
    items: Iterable[Mapping[str, Any]],
    day: str,
    *,
    raw_close: float | None = None,
    metrics: Iterable[str] | None = None,
) -> dict[str, MetricValue]:
    """按时点 day 由利润表季报计算成长 / 毛利率 / pe_ttm（只用 day 之前已可得的报告）。

    raw_close 为 day 前一交易日的不复权收盘（只影响 pe_ttm；None → pe_ttm 缺失）。
    """
    wanted = list(metrics) if metrics is not None else sorted(INCOME_METRICS)
    reports = [r for i, it in enumerate(items) if (r := _parse_report(it, i)) is not None]
    vals = _income_values(_FinHistory(reports), day, wanted, raw_close, "未提供不复权收盘价")
    return {
        m: MetricValue(metric=m, status=mv.status, value=mv.value, reason=mv.reason)  # type: ignore[arg-type]
        for m, mv in vals.items()
    }


# ───────────────────────────── 内部结构 ─────────────────────────────


class _InputError(ValueError):
    """输入不足以回测（返回 failed 结果，而不是抛给调用方）。"""


@dataclass(frozen=True, slots=True)
class _MV:
    status: _MStatus
    value: float | bool | None = None
    reason: str | None = None


def _ok(v: float | bool) -> _MV:
    return _MV("ok", v)


def _miss(reason: str) -> _MV:
    return _MV("missing", None, reason)


def _na(reason: str) -> _MV:
    return _MV("not_applicable", None, reason)


@dataclass(frozen=True, slots=True)
class _Report:
    year: int
    quarter: int  # 1..4（FY 视为 4）
    period_end: str
    available: str  # 可用日期：报告期末 < 披露日 ≤ 法定截止日时取披露日，否则取法定截止日
    fallback: bool  # True = 按法定截止日视为可用
    values: Mapping[str, float | None]
    order: int

    @property
    def label(self) -> str:
        return f"{self.year}年{_QUARTER_LABEL[self.quarter]}"


class _FinHistory:
    """单只股票的利润表历史，支持按时点取“当时已可得”的各期报告。"""

    def __init__(self, reports: list[_Report]) -> None:
        self._reports = sorted(reports, key=lambda r: (r.available, r.order))

    def __bool__(self) -> bool:
        return bool(self._reports)

    def as_of(self, day: str) -> dict[tuple[int, int], _Report]:
        """可用日期严格早于 day 的报告；同一报告期有多个版本时取当时最新的一版。"""
        out: dict[tuple[int, int], _Report] = {}
        for r in self._reports:
            if r.available >= day:
                break
            out[(r.year, r.quarter)] = r
        return out

    @property
    def fallback_count(self) -> int:
        return sum(1 for r in self._reports if r.fallback)


@dataclass(frozen=True, slots=True)
class _Plan:
    """一条可回测条件的执行计划。"""

    cid: str
    label: str
    metric: str
    op: str
    v1: float | None
    v2: float | None
    hard: bool
    weight: float
    is_rank: bool
    positive_only: bool


@dataclass(slots=True)
class _Arrays:
    close: np.ndarray  # 后复权收盘（对齐交易日历；缺失、非正、零成交记录为 NaN）
    close_ff: np.ndarray  # 前值填充后的后复权收盘
    raw_ff: np.ndarray | None  # 前值填充后的不复权收盘；None = 未获取
    turnover: np.ndarray | None  # 成交额（元）；None = 未获取
    bars: np.ndarray  # 有效交易日（close 非 NaN）在日历中的下标，升序


@dataclass(frozen=True, slots=True)
class _Flags:
    any_income: bool
    any_raw: bool
    any_turnover: bool
    bench_ok: bool
    any_list_date: bool
    any_name: bool


@dataclass
class _Ctx:
    req: BacktestRequest
    strategy: Strategy
    cal: list[str]
    i0: int
    i1: int
    rebalances: list[int]
    codes: list[str]
    arrays: dict[str, _Arrays]
    bench_ff: np.ndarray
    bench_ret_ok: np.ndarray  # 基准原始收盘（未填充，NaN = 缺失），用于 Beta 配对
    fin: dict[str, _FinHistory]
    info: dict[str, StockInfo]
    plans: list[_Plan]
    ignored: list[IgnoredCondition]
    sort_metric: str | None
    needed: list[str]
    top_n: int | None
    cost_rate: float
    notes: list[str]  # 规划阶段产生的口径说明
    dropped_dates: int
    fallback_reports: int


@dataclass(slots=True)
class _Selection:
    holdings: list[str]
    candidates: int
    failed: int
    missing: int
    no_price: int
    truncated: bool


@dataclass
class _Sim:
    nav: list[float]
    records: list[RebalanceRecord]
    turnovers: list[float]
    carried_days: int
    stale_fills: int


# ───────────────────────────── 主入口 ─────────────────────────────


def run_backtest(
    req: BacktestRequest,
    data: BacktestData,
    progress: Callable[[float, str], None] | None = None,
    *,
    backtest_id: str,
) -> BacktestResult:
    """执行一次时点一致的回测。纯函数：同输入同输出（created_at / finished_at 除外）。

    输入不足（区间内无交易日、股票池为空、无任何个股日K、日期格式错误）时返回 status="failed" 的结果。
    """
    created = _now()
    report: ProgressFn = progress if progress is not None else _noop
    report(0.0, "准备回测数据")
    try:
        ctx = _build_context(req, data)
    except _InputError as exc:
        return BacktestResult(
            id=backtest_id,
            status="failed",
            progress=1.0,
            progress_text="回测失败",
            request=req,
            caveats=_caveats(req),
            datasets=list(data.datasets),
            error=str(exc),
            created_at=created,
            finished_at=_now(),
        )
    report(0.05, f"共 {len(ctx.rebalances)} 个调仓日，开始逐期判定")
    sim = _simulate(ctx, report)
    report(0.95, "计算净值与统计")
    result = _assemble(ctx, sim, data, backtest_id, created)
    report(1.0, "回测完成")
    return result


# ───────────────────────────── 准备阶段 ─────────────────────────────


def _build_context(req: BacktestRequest, data: BacktestData) -> _Ctx:
    cal = sorted({_iso(d, "交易日历") for d in data.trade_dates})
    if len(cal) < 2:
        raise _InputError("基准指数日K（交易日历）不足 2 个交易日，无法回测。")
    start = _iso(req.start, "开始")
    end = _iso(req.end, "结束") if req.end else cal[-1]
    if start > end:
        if req.end:
            raise _InputError(f"开始日期 {start} 晚于结束日期 {end}。")
        raise _InputError(f"开始日期 {start} 晚于交易日历最后一个交易日 {end}（交易日历覆盖 {cal[0]} ~ {cal[-1]}）。")
    i0 = bisect_left(cal, start)
    i1 = bisect_right(cal, end) - 1
    if i0 >= len(cal) or i1 < i0 + 1:
        raise _InputError(f"回测区间 {start} ~ {end} 内交易日不足 2 个（交易日历覆盖 {cal[0]} ~ {cal[-1]}）。")
    codes = sorted({c for c in data.members if c})
    if not codes:
        raise _InputError("股票池为空，无法回测。")

    idx = {d: i for i, d in enumerate(cal)}
    arrays: dict[str, _Arrays] = {}
    dropped = 0
    for code in codes:
        series = data.prices.get(code)
        if series is not None:
            arrays[code], k = _align(series, idx, cal)
            dropped += k
    if not any(bool(np.any(~np.isnan(a.close))) for a in arrays.values()):
        raise _InputError("股票池内没有任何个股历史日K（数据集未获取或全部请求失败），无法回测。")

    bench_map: dict[str, float] = {}
    for k_, v_ in data.benchmark_close.items():
        try:
            bench_map[_iso(k_, "基准")] = v_
        except _InputError:
            continue
    bench = np.array([_pos(bench_map.get(d)) for d in cal], dtype=float)

    fin: dict[str, _FinHistory] = {}
    for code in codes:
        items = data.income.get(code) or []
        fin[code] = _FinHistory([r for i, it in enumerate(items) if (r := _parse_report(it, i)) is not None])

    info = {c: data.info[c] for c in codes if c in data.info}
    flags = _Flags(
        any_income=any(bool(h) for h in fin.values()),
        any_raw=any(a.raw_ff is not None and bool(np.any(~np.isnan(a.raw_ff))) for a in arrays.values()),
        any_turnover=any(a.turnover is not None and bool(np.any(~np.isnan(a.turnover))) for a in arrays.values()),
        bench_ok=int(np.sum(~np.isnan(bench))) >= 2,
        any_list_date=any(bool(i.list_date) for i in info.values()),
        any_name=any(bool(i.name) for i in info.values()),
    )

    plans, ignored, notes = _plan_conditions(req.strategy, flags)
    sort_metric = _plan_sort(req.strategy, flags, notes)
    needed = sorted({p.metric for p in plans} | ({sort_metric} if sort_metric else set()))

    top_n = req.top_n if req.top_n is not None and req.top_n > 0 else None
    if req.top_n is not None and req.top_n <= 0:
        notes.append(f"top_n={req.top_n} 无效，按不限制持仓数处理。")
    cost_bps = req.cost_bps if math.isfinite(req.cost_bps) and req.cost_bps > 0 else 0.0
    if req.cost_bps < 0:
        notes.append("cost_bps 为负，交易成本按 0 计。")

    return _Ctx(
        req=req,
        strategy=req.strategy,
        cal=cal,
        i0=i0,
        i1=i1,
        rebalances=_rebalance_indices(cal, i0, i1, req.rebalance),
        codes=codes,
        arrays=arrays,
        bench_ff=_ffill(bench),
        bench_ret_ok=bench,
        fin=fin,
        info=info,
        plans=plans,
        ignored=ignored,
        sort_metric=sort_metric,
        needed=needed,
        top_n=top_n,
        cost_rate=cost_bps / 1e4,
        notes=notes,
        dropped_dates=dropped,
        fallback_reports=sum(h.fallback_count for h in fin.values()),
    )


def _rebalance_indices(cal: Sequence[str], i0: int, i1: int, rebalance: str) -> list[int]:
    out: list[int] = []
    last: tuple[str, int] | None = None
    for t in range(i0, i1 + 1):
        d = cal[t]
        month = int(d[5:7])
        key = (d[:4], month if rebalance == "monthly" else (month - 1) // 3)
        if key != last:
            out.append(t)
            last = key
    if len(out) > 1 and out[-1] == i1:
        out.pop()  # 区间最后一个交易日不调仓
    return out


def _align(series: StockSeries, idx: Mapping[str, int], cal: Sequence[str]) -> tuple[_Arrays, int]:
    """把个股序列对齐到交易日历（向量化）；返回对齐结果与被忽略的日期数。"""
    n = len(cal)
    rows = np.full(len(series.dates), -1, dtype=np.int64)
    dropped = 0
    for j, d in enumerate(series.dates):
        i = idx.get(d)
        if i is None:
            norm = _normalize_loose(d)
            i = idx.get(norm) if norm else None
            if i is None:
                if norm is None or cal[0] <= norm <= cal[-1]:
                    dropped += 1  # 日期无法解析，或落在日历区间内却不在日历中
                continue
        rows[j] = i
    keep = rows >= 0
    target = rows[keep]

    def place(seq: Sequence[float | None], *, positive: bool) -> np.ndarray:
        out = np.full(n, np.nan)
        out[target] = _float_array(seq, positive=positive)[keep]
        return out

    close = place(series.close, positive=True)
    raw = place(series.close_raw, positive=True) if series.close_raw is not None else None
    turnover = place(series.turnover, positive=False) if series.turnover is not None else None
    if turnover is not None:
        close[turnover == 0] = np.nan  # 零成交 = 停牌记录，与筛选口径（features.trading_bars）一致
    bars = np.flatnonzero(~np.isnan(close))
    return _Arrays(close, _ffill(close), _ffill(raw) if raw is not None else None, turnover, bars), dropped


def _float_array(seq: Sequence[Any], *, positive: bool) -> np.ndarray:
    """None/非数值/非有限 → NaN；positive=True 时非正值也视为缺失，否则负值视为缺失。"""
    try:
        a = np.array(seq, dtype=float)
    except (TypeError, ValueError):
        a = np.array([float(x) if is_finite_number(x) else np.nan for x in seq], dtype=float)
    a[~np.isfinite(a)] = np.nan
    a[(a <= 0) if positive else (a < 0)] = np.nan
    return a


def _plan_conditions(strategy: Strategy, flags: _Flags) -> tuple[list[_Plan], list[IgnoredCondition], list[str]]:
    plans: list[_Plan] = []
    ignored: list[IgnoredCondition] = []
    notes: list[str] = []
    disabled = 0
    for i, c in enumerate(strategy.conditions):
        if not c.enabled:
            disabled += 1
            continue
        cid = c.id or f"c{i + 1}"
        label = c.label or _describe(c)
        reason = _ignore_reason(c)
        if reason:
            ignored.append(IgnoredCondition(condition_id=cid, label=label, reason=reason))
            continue
        absent = _data_absent_reason(c.metric, flags)
        if absent:  # 数据整体缺失：条件保留，逐股按缺失处理（绝不因数据缺失而放行）
            effect = "硬条件下所有股票不入选" if c.hard else "软条件下所有股票不得分"
            notes.append(f"条件「{label}」所需数据不可得：{absent}；该条件对所有股票按缺失处理（{effect}），不会视为通过。")
        md = METRICS[c.metric]
        is_rank = c.op in RANK_OPS
        if is_rank and c.rank_scope != "universe":
            scope = RANK_SCOPE_LABEL.get(c.rank_scope or "", c.rank_scope or "")
            notes.append(
                f"条件「{label}」的{scope}分位在回测中降级为调仓日股票池内分位（历史行业归属与全市场数据不可得）。"
            )
        plans.append(
            _Plan(
                cid=cid,
                label=label,
                metric=c.metric,
                op=c.op,
                v1=float(c.value) if c.value is not None else None,
                v2=float(c.value2) if c.value2 is not None else None,
                hard=c.hard,
                weight=max(float(c.weight), 0.0) if math.isfinite(c.weight) else 0.0,
                is_rank=is_rank,
                positive_only=md.positive_only_for_rank,
            )
        )
    if disabled:
        notes.append(f"已停用的 {disabled} 条条件不参与回测。")
    return plans, ignored, notes


def _ignore_reason(c: Condition) -> str | None:
    """条件本身无法回测（指标不支持、算子不合法、缺阈值）的原因；None = 可回测。"""
    md = METRICS.get(c.metric)
    if md is None:
        return f"指标「{c.metric}」不在指标目录中，无法回测。"
    if c.metric not in SUPPORTED:
        return _not_backtestable_reason(md)
    if c.op not in md.allowed_ops:
        return f"算子「{_OP_SYMBOL.get(c.op, c.op)}」不适用于「{md.name}」，无法判定。"
    if c.op in _NUMERIC_OPS or c.op in RANK_OPS:
        if not is_finite_number(c.value):
            return "缺少有效阈值，无法判定。"
        if c.op == "between" and not is_finite_number(c.value2):
            return "“介于”条件缺少上界阈值，无法判定。"
    if c.op in RANK_OPS:  # 与筛选侧结构校验（conditions.structural_problems）一致
        if not 0 <= float(c.value) <= 100:  # type: ignore[arg-type]
            return f"分位阈值 {_fmt(c.value)} 不在 0~100 之间，无法判定。"
        if c.rank_scope is None:
            return "分位条件未指定参考范围，无法判定。"
        if c.rank_scope not in md.rank_scopes:
            return f"「{md.name}」不支持{RANK_SCOPE_LABEL.get(c.rank_scope, c.rank_scope)}分位，无法判定。"
    return None


def _not_backtestable_reason(md: MetricDef) -> str:
    if md.id in _NOT_BACKTESTABLE:
        return _NOT_BACKTESTABLE[md.id]
    if md.category in ("profitability", "quality", "solvency"):
        return "该指标来自财务指标接口的单期数据，回测未重建其历史时点序列。"
    return "指标目录未开放该指标的历史回测。"


def _data_absent_reason(metric: str, flags: _Flags) -> str | None:
    """指标所需数据在整个股票池都不可得（而非个别股票缺失）的说明；None = 至少部分可得。"""
    if metric == "beta_250d" and not flags.bench_ok:
        return "基准指数收盘价缺失"
    if metric == "avg_amount_20d" and not flags.any_turnover:
        return "个股成交额全部缺失（未获取或请求失败）"
    if metric in INCOME_METRICS and not flags.any_income:
        return "股票池内没有任何利润表季报数据（未获取或全部请求失败）"
    if metric == "pe_ttm" and not flags.any_raw:
        return "不复权收盘价全部缺失（未获取或请求失败），无法近似重建历史市盈率TTM"
    if metric == "listing_days" and not flags.any_list_date:
        return "股票池内没有任何上市日期数据"
    if metric == "is_st" and not flags.any_name:
        return "股票池内没有任何证券简称数据"
    return None


def _plan_sort(strategy: Strategy, flags: _Flags, notes: list[str]) -> str | None:
    sm = strategy.sort_metric
    if not sm:
        return None
    md = METRICS.get(sm)
    name = md.name if md else sm
    if md is None or sm not in SUPPORTED or md.value_type != "number":
        notes.append(f"排序指标「{name}」不支持回测，候选超过 top_n 时只按软条件得分与代码排序。")
        return None
    reason = _data_absent_reason(sm, flags)
    if reason:
        notes.append(f"排序指标「{name}」无法计算（{reason}），候选超过 top_n 时只按软条件得分与代码排序。")
        return None
    return sm


# ───────────────────────────── 逐期模拟 ─────────────────────────────


def _simulate(ctx: _Ctx, report: ProgressFn) -> _Sim:
    reb = set(ctx.rebalances)
    total = len(ctx.rebalances)
    nav: list[float] = []
    records: list[RebalanceRecord] = []
    turnovers: list[float] = []
    units: dict[str, float] = {}
    cash = 1.0
    carried = stale = 0
    k = 0
    for t in range(ctx.i0, ctx.i1 + 1):
        value = cash
        for code, u in units.items():
            a = ctx.arrays[code]
            if math.isnan(a.close[t]):
                carried += 1  # 当日无价格，沿用前值估值
            value += u * float(a.close_ff[t])
        nav.append(value)
        if t not in reb:
            continue
        k += 1
        day = ctx.cal[t]
        report(0.05 + 0.9 * (k - 1) / max(total, 1), f"调仓 {k}/{total}（{day}）")
        sel = _select(ctx, t)

        old_w = {c: u * float(ctx.arrays[c].close_ff[t]) / value for c, u in units.items()} if value > 0 else {}
        n = len(sel.holdings)
        new_w = {c: 1.0 / n for c in sel.holdings} if n else {}
        traded = sum(abs(new_w.get(c, 0.0) - old_w.get(c, 0.0)) for c in sorted(set(old_w) | set(new_w)))
        cash_old = cash / value if value > 0 else 1.0
        one_way = 0.5 * (traded + abs((0.0 if n else 1.0) - cash_old))
        cost = traded * ctx.cost_rate
        capital = value * (1.0 - cost)
        if n:
            units = {c: capital / n / float(ctx.arrays[c].close_ff[t]) for c in sel.holdings}
            cash = 0.0
            stale += sum(1 for c in sel.holdings if math.isnan(ctx.arrays[c].close[t]))
        else:
            units = {}
            cash = capital
        turnovers.append(one_way)
        records.append(
            RebalanceRecord(
                date=day,
                holdings=list(sel.holdings),
                names=[ctx.info[c].name if c in ctx.info else None for c in sel.holdings],
                candidates=sel.candidates,
                note=_record_note(ctx, sel, one_way, cost, first=(k == 1)),
            )
        )
    return _Sim(nav=nav, records=records, turnovers=turnovers, carried_days=carried, stale_fills=stale)


def _select(ctx: _Ctx, t: int) -> _Selection:
    values = _metric_values(ctx, t)
    states = [_evaluate_plan(p, values[p.metric], ctx.codes) for p in ctx.plans]
    hard_idx = [i for i, p in enumerate(ctx.plans) if p.hard]
    soft = [(i, p.weight) for i, p in enumerate(ctx.plans) if not p.hard]
    soft_total = sum(w for _, w in soft)

    failed = missing = no_price = 0
    cands: list[str] = []
    for code in ctx.codes:
        hs = [states[i][code] for i in hard_idx]
        if any(s == "fail" for s in hs):
            failed += 1
        elif any(s != "pass" for s in hs):
            missing += 1  # 缺失 / 不适用 → 不入选
        elif code not in ctx.arrays or math.isnan(ctx.arrays[code].close_ff[t]):
            no_price += 1
        else:
            cands.append(code)

    def score(code: str) -> float:
        if soft_total <= 0:
            return 0.0
        return round(sum(w for i, w in soft if states[i][code] == "pass") / soft_total, 6)  # 与 evaluate 一致

    def sort_key(code: str) -> tuple[float, int, float, str]:
        sk: tuple[int, float] = (0, 0.0)
        if ctx.sort_metric:
            mv = values[ctx.sort_metric][code]
            if mv.status == "ok" and mv.value is not None:
                v = float(mv.value)
                sk = (0, -v if ctx.strategy.sort_desc else v)
            else:
                sk = (1, 0.0)  # 排序指标缺失的排在最后
        return (-score(code), sk[0], sk[1], code)

    ordered = sorted(cands, key=sort_key)
    truncated = ctx.top_n is not None and len(ordered) > ctx.top_n
    holdings = ordered[: ctx.top_n] if truncated else ordered
    return _Selection(holdings, len(cands), failed, missing, no_price, truncated)


def _evaluate_plan(p: _Plan, vals: Mapping[str, _MV], codes: Sequence[str]) -> dict[str, _CState]:
    out: dict[str, _CState] = {}
    if p.is_rank:
        assert p.v1 is not None
        group = sorted(
            float(mv.value)  # type: ignore[arg-type]
            for mv in (vals[c] for c in codes)
            if mv.status == "ok" and (not p.positive_only or float(mv.value) > 0)  # type: ignore[arg-type]
        )
        for c in codes:
            mv = vals[c]
            if mv.status != "ok":
                out[c] = mv.status
            elif p.positive_only and float(mv.value) <= 0:  # type: ignore[arg-type]
                out[c] = "not_applicable"
            elif len(group) < MIN_RANK_GROUP:
                out[c] = "not_applicable"
            else:
                rank = round(pct_rank(group, float(mv.value)), 4)  # type: ignore[arg-type]  # 与 evaluate 一致
                ok = rank <= p.v1 if p.op == "pct_rank_lte" else rank >= p.v1
                out[c] = "pass" if ok else "fail"
        return out
    for c in codes:
        mv = vals[c]
        if mv.status != "ok":
            out[c] = mv.status
        else:
            out[c] = "pass" if check_op(p.op, mv.value, p.v1, p.v2) else "fail"
    return out


def _metric_values(ctx: _Ctx, t: int) -> dict[str, dict[str, _MV]]:
    """调仓日（日历下标 t）各所需指标的逐股取值；价格类截至 t−1，财报只用可用日期早于调仓日的报告。"""
    day = ctx.cal[t]
    cut = t - 1
    out: dict[str, dict[str, _MV]] = {m: {} for m in ctx.needed}
    fin_needed = [m for m in ctx.needed if m in INCOME_METRICS]
    need_raw = "pe_ttm" in fin_needed
    for code in ctx.codes:
        arr = ctx.arrays.get(code)
        if fin_needed:
            raw, raw_reason = _raw_close_at(arr, cut) if need_raw else (None, "")
            fv = _income_values(ctx.fin[code], day, fin_needed, raw, raw_reason)
            for m in fin_needed:
                out[m][code] = fv[m]
        for m in ctx.needed:
            if m in PRICE_WINDOWS:
                out[m][code] = _price_metric(m, arr, cut, ctx.bench_ret_ok)
            elif m == "listing_days":
                out[m][code] = _listing_days(ctx.info.get(code), day)
            elif m == "is_st":
                out[m][code] = _is_st(ctx.info.get(code))
    return out


# ───────────────────────────── 指标计算 ─────────────────────────────


def _price_metric(metric: str, arr: _Arrays | None, cut: int, bench: np.ndarray) -> _MV:
    """窗口 = 该股截至 cut（调仓日前一交易日）最近的有效日K，与筛选口径一致：
    近 N 日指标取 N+1 根（N 个区间），日均成交额取 N 根；区间/样本数 < ⌈0.8N⌉ → 缺失。"""
    kind, n = PRICE_WINDOWS[metric]
    if arr is None:
        return _miss("无该股历史日K（未获取或请求失败）")
    need = math.ceil(MIN_SAMPLE_RATIO * n)
    m = int(np.searchsorted(arr.bars, cut, side="right")) if cut >= 0 else 0  # 截至 cut 的有效日K数
    if kind == "amount":
        if arr.turnover is None:
            return _miss("未获取该股成交额")
        idx = arr.bars[max(0, m - n) : m]
        vals = arr.turnover[idx]
        vals = vals[~np.isnan(vals)]
        if vals.size < need:
            return _miss(f"成交额样本不足：调仓日前可用 {vals.size} 个交易日，少于 {n}×{MIN_SAMPLE_RATIO:g}={need}")
        return _ok(float(np.mean(vals)) / 1e8)

    idx = arr.bars[max(0, m - n - 1) : m]
    k = max(idx.size - 1, 0)
    if k < need:
        return _miss(f"价格样本不足：调仓日前可用 {k} 个交易日，少于 {n}×{MIN_SAMPLE_RATIO:g}={need}")
    w = arr.close[idx]
    if kind == "beta":
        paired = idx[~np.isnan(bench[idx])]  # 与基准按日期对齐
        k = max(paired.size - 1, 0)
        if k < need:
            return _miss(f"个股与基准按日期对齐后样本不足：{k} 个，少于 {n}×{MIN_SAMPLE_RATIO:g}={need}")
        s, b = arr.close[paired], bench[paired]
        beta = beta_coefficient(s[1:] / s[:-1] - 1.0, b[1:] / b[:-1] - 1.0)
        return _ok(beta) if beta is not None else _miss("基准日收益方差为 0，无法计算 Beta")
    if kind == "vol":
        res = annualized_volatility(w)
    elif kind == "mdd":
        res = max_drawdown_pct(w)
    else:
        res = period_return_pct(w)
    return _ok(res) if res is not None else _miss("价格序列无效")


def _raw_close_at(arr: _Arrays | None, cut: int) -> tuple[float | None, str]:
    if arr is None:
        return None, "无该股历史日K（未获取或请求失败）"
    if arr.raw_ff is None:
        return None, "未获取该股不复权收盘价"
    if cut < 0 or math.isnan(arr.raw_ff[cut]):
        return None, "调仓日前没有不复权收盘价"
    return float(arr.raw_ff[cut]), ""


def _listing_days(info: StockInfo | None, day: str) -> _MV:
    if info is None or not info.list_date:
        return _miss("缺少上市日期")
    try:
        listed = date.fromisoformat(info.list_date[:10])
    except ValueError:
        return _miss(f"上市日期格式无效：{info.list_date}")
    return _ok(float((date.fromisoformat(day) - listed).days))


def _is_st(info: StockInfo | None) -> _MV:
    if info is None or not info.name:
        return _miss("缺少证券简称")
    return _ok("ST" in info.name.upper())


def _income_values(hist: _FinHistory, day: str, metrics: Sequence[str], raw: float | None, raw_reason: str) -> dict[str, _MV]:
    if not metrics:
        return {}
    view = hist.as_of(day)
    if not view:
        reason = "调仓日前没有已披露的利润表季报" if hist else "无该股利润表数据（未获取或请求失败）"
        return {m: _miss(reason) for m in metrics}
    cur = view[max(view)]
    age = (date.fromisoformat(day) - date.fromisoformat(cur.period_end)).days
    if age > STALE_REPORT_DAYS:
        return {m: _miss(f"最新已披露报告（{cur.label}）距调仓日超过 {STALE_REPORT_DAYS} 天，视为过旧") for m in metrics}
    prev = view.get((cur.year - 1, cur.quarter))
    out: dict[str, _MV] = {}
    for m in metrics:
        if m in YOY_FIELDS:
            out[m] = _yoy(cur, prev, YOY_FIELDS[m])
        elif m == "gross_margin":
            out[m] = _gross_margin(cur)
        elif m == "gross_margin_change":
            g1 = _gross_margin(cur)
            if prev is None:
                out[m] = _miss(f"缺少上年同期（{cur.year - 1}年{_QUARTER_LABEL[cur.quarter]}）报告")
            elif g1.status != "ok":
                out[m] = g1
            else:
                g0 = _gross_margin(prev)
                out[m] = g0 if g0.status != "ok" else _ok(float(g1.value) - float(g0.value))  # type: ignore[arg-type]
        elif m == "pe_ttm":
            out[m] = _pe_ttm(cur, view, raw, raw_reason)
    return out


def _yoy(cur: _Report, prev: _Report | None, fld: str) -> _MV:
    name = _FIELD_LABEL[fld]
    v = cur.values.get(fld)
    if v is None:
        return _miss(f"{cur.label}{name}为空（未披露或不适用）")
    if prev is None:
        return _miss(f"缺少上年同期（{cur.year - 1}年{_QUARTER_LABEL[cur.quarter]}）报告")
    p = prev.values.get(fld)
    if p is None:
        return _miss(f"上年同期（{prev.label}）{name}为空")
    if p == 0:
        return _miss(f"上年同期（{prev.label}）{name}为 0，同比无意义")
    return _ok((v - p) / abs(p) * 100.0)


def _gross_margin(r: _Report) -> _MV:
    rev = r.values.get("operating_income")
    cost = r.values.get("operating_costs")
    if rev is None:
        return _miss(f"{r.label}营业收入为空")
    if cost is None:
        return _miss(f"{r.label}营业成本为空（未披露或不适用，如金融企业）")
    if rev <= 0:
        return _na(f"{r.label}营业收入非正，毛利率无意义")
    return _ok((rev - cost) / rev * 100.0)


def _pe_ttm(cur: _Report, view: Mapping[tuple[int, int], _Report], raw: float | None, raw_reason: str) -> _MV:
    eps = cur.values.get("basic_eps")
    if eps is None:
        return _miss(f"{cur.label}基本每股收益为空")
    if cur.quarter == 4:
        ttm = eps
    else:
        annual = view.get((cur.year - 1, 4))
        same = view.get((cur.year - 1, cur.quarter))
        a_eps = annual.values.get("basic_eps") if annual else None
        s_eps = same.values.get("basic_eps") if same else None
        if a_eps is None:
            return _miss(f"缺少上年年报（{cur.year - 1}年年报）基本每股收益，无法计算 EPS_TTM")
        if s_eps is None:
            return _miss(f"缺少上年同期（{cur.year - 1}年{_QUARTER_LABEL[cur.quarter]}）基本每股收益，无法计算 EPS_TTM")
        ttm = eps + a_eps - s_eps
    if raw is None:
        return _miss(raw_reason or "缺少不复权收盘价")
    if abs(ttm) < 1e-12:
        return _miss("EPS_TTM 为 0，市盈率无意义")
    return _ok(raw / ttm)


# ───────────────────────────── 利润表解析 ─────────────────────────────


def _parse_report(item: Mapping[str, Any], order: int) -> _Report | None:
    if not isinstance(item, Mapping):
        return None
    period_end = _ms_to_date(item.get("period_end_ms"))
    quarter = _quarter(item.get("fiscal_period"), period_end)
    year = _int_or_none(item.get("fiscal_year"))
    if year is None and period_end:
        year = int(period_end[:4])
    if quarter is None or year is None:
        return None
    if period_end is None:
        m, d = _PERIOD_END_MD[quarter]
        period_end = date(year, m, d).isoformat()
    dy, m, d = _DEADLINE[quarter]
    deadline = date(year + dy, m, d).isoformat()
    disclosed = _ms_to_date(item.get("report_date_ms"))
    if disclosed is not None and period_end < disclosed <= deadline:
        available, fallback = disclosed, False
    else:
        # 实测：历史期的 report_date_ms 会被后续报告覆盖（如 2025Q2 显示 2026-08-15），
        # 晚于法定截止日或缺失时一律按法定披露截止日视为可得
        available, fallback = deadline, True
    values = {f: _num(item.get(f)) for f in _INCOME_FIELDS}
    return _Report(year, quarter, period_end, available, fallback, values, order)


def _quarter(fiscal_period: Any, period_end: str | None) -> int | None:
    if isinstance(fiscal_period, str):
        s = fiscal_period.strip().upper()
        if s in ("Q1", "Q2", "Q3", "Q4"):
            return int(s[1])
        if s == "FY":
            return 4
    if period_end:
        return {3: 1, 6: 2, 9: 3, 12: 4}.get(int(period_end[5:7]))
    return None


# ───────────────────────────── 结果组装 ─────────────────────────────


def _assemble(ctx: _Ctx, sim: _Sim, data: BacktestData, backtest_id: str, created: datetime) -> BacktestResult:
    days = ctx.cal[ctx.i0 : ctx.i1 + 1]
    base = float(ctx.bench_ff[ctx.i0])
    bench_ok = math.isfinite(base) and base > 0
    bench_vals = [float(ctx.bench_ff[t]) / base for t in range(ctx.i0, ctx.i1 + 1)] if bench_ok else []
    curve = [
        CurvePoint(date=d, strategy=round(sim.nav[j], 6), benchmark=round(bench_vals[j], 6) if bench_ok else None)
        for j, d in enumerate(days)
    ]

    stats = perf_stats(sim.nav)
    bstats = perf_stats(bench_vals) if bench_ok else None
    s_total = (sim.nav[-1] / sim.nav[0] - 1.0) * 100.0
    if bstats is not None and bench_vals:
        stats.excess_total_return = _r4(s_total - (bench_vals[-1] / bench_vals[0] - 1.0) * 100.0)
    if sim.records:
        stats.avg_holdings = _r4(sum(len(r.holdings) for r in sim.records) / len(sim.records))
    if len(sim.turnovers) > 1:
        stats.avg_turnover = _r4(sum(sim.turnovers[1:]) / (len(sim.turnovers) - 1) * 100.0)

    return BacktestResult(
        id=backtest_id,
        status="done",
        progress=1.0,
        progress_text="回测完成",
        request=ctx.req,
        curve=curve,
        stats=stats,
        benchmark_stats=bstats,
        rebalances=sim.records,
        ignored_conditions=ctx.ignored,
        assumptions=_assumptions(ctx, sim, data, bench_ok),
        caveats=_caveats(ctx.req),
        datasets=list(data.datasets),
        error=None,
        created_at=created,
        finished_at=_now(),
    )


def _record_note(ctx: _Ctx, sel: _Selection, one_way: float, cost: float, *, first: bool) -> str:
    parts: list[str] = []
    if sel.holdings:
        parts.append(f"{'建仓：' if first else ''}满足全部硬条件且可成交 {sel.candidates} 只")
        if sel.truncated:
            parts.append(f"超过 top_n，按{_order_text(ctx)}取前 {ctx.top_n} 只")
    else:
        parts.append(f"{'建仓：' if first else ''}无满足全部硬条件且可成交的股票，本期空仓")
    if sel.failed:
        parts.append(f"{sel.failed} 只未通过硬条件")
    if sel.missing:
        parts.append(f"{sel.missing} 只因数据缺失或不适用未入选")
    if sel.no_price:
        parts.append(f"{sel.no_price} 只满足条件但调仓日前无可用价格，未纳入")
    parts.append(f"单边换手 {one_way * 100:.1f}%，交易成本 {cost * 100:.3f}%")
    return "；".join(parts)


def _order_text(ctx: _Ctx) -> str:
    seq = ["软条件得分降序"]
    if ctx.sort_metric:
        seq.append(f"{METRICS[ctx.sort_metric].name}{'降序' if ctx.strategy.sort_desc else '升序'}")
    seq.append("代码升序")
    return " → ".join(seq)


def _assumptions(ctx: _Ctx, sim: _Sim, data: BacktestData, bench_ok: bool) -> list[str]:
    req = ctx.req
    metrics = {p.metric for p in ctx.plans} | ({ctx.sort_metric} if ctx.sort_metric else set())
    n_days = ctx.i1 - ctx.i0 + 1
    freq = "每月" if req.rebalance == "monthly" else "每季"
    out = [
        f"回测区间：{ctx.cal[ctx.i0]} ~ {ctx.cal[ctx.i1]}（{n_days} 个交易日，交易日历取自基准指数日K）；"
        f"股票池为当前成分股 {len(ctx.codes)} 只。",
        f"调仓：区间内首个交易日建仓，其后{freq}首个交易日调仓，共 {len(ctx.rebalances)} 次；区间最后一个交易日不再调仓。",
        "时点规则：调仓日只用前一交易日（含）及以前的行情数据、以及此前已披露的财报判定条件；以调仓日收盘价（后复权）成交。",
        "持仓：满足全部硬条件的股票等权持有，软条件只用于排序；硬条件数据缺失或不适用的股票一律不入选；"
        "两次调仓之间不再平衡（权重随价格漂移）。",
        f"候选超过 top_n 时按{_order_text(ctx)}截取。"
        if ctx.top_n is not None
        else "未限制持仓数（top_n 为空），满足条件的股票全部等权持有。",
        f"交易成本：买卖每边按 {ctx.cost_rate * 1e4:g} bps 计，成本 =（买入比例 + 卖出比例）× 费率，"
        "在调仓日收盘从净值扣除；首次建仓同样计费；基准不计成本。",
        "净值曲线每个交易日一点：调仓日的点为调仓前持仓市值，新持仓与交易成本自下一交易日起体现；"
        "首点为初始资金 1.0，基准同日归一为 1.0。",
        f"个股某日无价格（停牌或数据缺失）时沿用前一可用收盘价估值；本次共 {sim.carried_days} 个持仓·日使用沿用价格，"
        f"{sim.stale_fills} 笔调仓成交因调仓日无收盘价而按沿用价格计（未模拟停牌无法成交）。",
        "统计口径：年化按 252 个交易日复利折算；年化波动率 = 日收益样本标准差 × √252；夏普比率的无风险利率取 0；"
        "最大回撤以正数表示；超额收益 = 策略总收益 − 基准总收益；平均持仓数按各次调仓平均；平均单边换手不含首次建仓。",
    ]
    no_hist = sum(1 for c in ctx.codes if c not in ctx.arrays or not bool(np.any(~np.isnan(ctx.arrays[c].close))))
    if no_hist:
        out.append(f"股票池中 {no_hist} 只没有任何历史日K（未获取或请求失败），价格类条件按缺失处理且不可成交，不会入选。")
    if ctx.strategy.missing_policy == "keep_flagged":
        out.append("策略的缺失处理为“保留并标记”，回测中不采用：硬条件数据缺失或不适用的股票一律不入选。")
    if ctx.strategy.report_period:
        out.append(f"策略指定的报告期（{ctx.strategy.report_period}）在回测中不适用：各调仓日使用当时已披露的最新报告。")
    if not ctx.plans:
        out.append("没有可回测的条件：候选为股票池内全部可成交股票。")
    elif not any(p.hard for p in ctx.plans):
        out.append("没有可回测的硬条件：候选为股票池内全部可成交股票，软条件只影响排序。")
    if ctx.top_n is not None and not ctx.sort_metric and not any(not p.hard for p in ctx.plans):
        out.append("未设置可回测的软条件与排序指标：候选超过 top_n 时按代码顺序截取，与指标数值无关。")

    price_used = sorted(m for m in metrics if m in PRICE_WINDOWS)
    if price_used:
        names = "、".join(METRICS[m].name for m in price_used)
        out.append(
            f"价格类指标（{names}）与筛选口径一致：取该股截至调仓日前一交易日（含）最近的有效后复权日K"
            "（剔除缺价与零成交的停牌记录），近 N 日指标取 N+1 根收盘（N 个区间），日均成交额取近 20 根；"
            "样本少于 N × 80% 视为缺失（不入选）；波动率用对数收益样本标准差。"
        )
        longest = max(PRICE_WINDOWS[m][1] for m in price_used) + 1
        if ctx.i0 - 1 < longest - 1:
            out.append(
                f"首个调仓日前只有 {ctx.i0} 个交易日的历史，早期调仓日的长窗口指标可能样本不足（相关股票不入选）。"
            )
    if "beta_250d" in metrics:
        out.append(f"Beta 用同一窗口内个股与基准（{req.benchmark}）按日期对齐后的简单收益计算 cov ÷ var。")
        if req.benchmark != SCREEN_BETA_BENCHMARK:
            out.append(f"注意：回测 Beta 以回测基准 {req.benchmark} 计算，筛选口径为相对沪深300（{SCREEN_BETA_BENCHMARK}）。")
    fin_used = sorted(m for m in metrics if m in INCOME_METRICS)
    if fin_used:
        out.append(
            "财报时点：每只股票只使用可用日期早于调仓日的利润表季报，取其中最新一期（各股报告期可能不同）；"
            "披露日缺失、不晚于报告期末或晚于法定截止日时（实测数据源会用后续报告日期覆盖历史期披露日），按法定披露截止日（一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30）视为可用"
            f"（本次共 {ctx.fallback_reports} 条）；最新报告期末距调仓日超过 {STALE_REPORT_DAYS} 天视为过旧（缺失）。"
            "财务数值为接口当前版本，若有追溯调整，可能与当时披露值不同。"
        )
    if any(m in YOY_FIELDS or m.startswith("gross_margin") for m in fin_used):
        out.append(
            "成长与毛利率为回测自算口径（由利润表累计值计算，可能与财务指标接口略有差异）："
            "同比 =（本期累计 − 上年同期累计）÷ |上年同期累计| × 100，上年同期缺失或为 0 视为缺失；"
            "毛利率 =（营业收入 − 营业成本）÷ 营业收入 × 100；净利润同比使用利润表 net_profit（净利润）字段。"
        )
    if "pe_ttm" in fin_used:
        out.append(
            "市盈率TTM 为近似重建：调仓日前一交易日不复权收盘价 ÷ EPS_TTM，EPS_TTM = 本期累计基本每股收益 + 上年年报 − 上年同期累计"
            "（本期为年报时取年报值）；送转等股本变动期间可能失真；亏损公司为负值。"
        )
    if "listing_days" in metrics:
        out.append("上市天数 = 调仓日 − 上市日期（自然日）。")
    if "is_st" in metrics:
        out.append("是否ST 按当前证券简称判断，未还原历史 ST 状态，与历史实际存在偏差。")
    out.extend(ctx.notes)
    if not bench_ok:
        out.append("基准指数收盘价缺失，基准曲线与基准统计为空。")
    if ctx.dropped_dates:
        out.append(f"有 {ctx.dropped_dates} 条个股日K的日期不在基准交易日历中，已忽略。")
    for ds in data.datasets:
        if ds.status in ("partial", "failed"):
            state = "部分失败" if ds.status == "partial" else "失败"
            out.append(
                f"数据集「{ds.label}」{state}（成功 {ds.succeeded}/{ds.requested}）：受影响股票的相关条件按缺失处理，不会视为通过。"
            )
    if n_days - 1 < 60:
        out.append("回测区间不足 60 个交易日，年化类指标波动大，仅供参考。")
    return out


def _caveats(req: BacktestRequest) -> list[str]:
    bps = req.cost_bps if math.isfinite(req.cost_bps) and req.cost_bps > 0 else 0.0
    return [
        "幸存者偏差：股票池使用当前成分股回溯历史，期间被调出或已退市的股票未纳入，结果可能偏乐观。",
        "历史回测不代表未来表现：结果只反映筛选规则在历史数据上的表现，不构成任何投资建议。",
        "未模拟涨跌停、停牌等导致的无法成交，按收盘价全部成交。",
        f"交易成本仅按固定费率估计（买卖每边 {bps:g} bps），未考虑冲击成本、滑点与税费差异。",
    ]


# ───────────────────────────── 小工具 ─────────────────────────────


def _describe(c: Condition) -> str:
    md = METRICS.get(c.metric)
    name = md.name if md else c.metric
    unit = md.unit if md else ""
    if c.op in ("is_true", "is_false"):
        return f"{name}：{'是' if c.op == 'is_true' else '否'}"
    if c.op in RANK_OPS:
        scope = RANK_SCOPE_LABEL.get(c.rank_scope or "universe", "股票池内")
        return f"{name} {scope}分位 {'≤' if c.op == 'pct_rank_lte' else '≥'} {_fmt(c.value)}"
    if c.op == "between":
        return f"{name} 介于 {_fmt(c.value)}{unit} ~ {_fmt(c.value2)}{unit}"
    return f"{name} {_OP_SYMBOL.get(c.op, c.op)} {_fmt(c.value)}{unit}"


def _fmt(v: float | None) -> str:
    return "?" if v is None else f"{v:g}"


def _r4(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, 4)


def _pos(x: Any) -> float:
    """有效正价格 → float，否则 NaN。"""
    if is_finite_number(x) and float(x) > 0:
        return float(x)
    return float("nan")


def _num(x: Any) -> float | None:
    return float(x) if is_finite_number(x) else None


def _int_or_none(x: Any) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float) and math.isfinite(x) and x.is_integer():
        return int(x)
    if isinstance(x, str) and x.strip().isdigit():
        return int(x.strip())
    return None


def _ms_to_date(ms: Any) -> str | None:
    if not is_finite_number(ms):
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=CN_TZ).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iso(s: Any, what: str) -> str:
    try:
        return date.fromisoformat(str(s)).isoformat()
    except ValueError:
        raise _InputError(f"{what}日期格式应为 yyyy-MM-dd，收到 {s!r}。") from None


def _normalize_loose(d: Any) -> str | None:
    try:
        return date.fromisoformat(str(d)).isoformat()
    except ValueError:
        return None


def _ffill(a: np.ndarray) -> np.ndarray:
    """前值填充；首个有效值之前保持 NaN。"""
    if a.size == 0:
        return a.copy()
    pos = np.where(~np.isnan(a), np.arange(a.size), 0)
    np.maximum.accumulate(pos, out=pos)
    return a[pos]


def _now() -> datetime:
    return datetime.now(CN_TZ)


def _noop(_: float, __: str) -> None:
    return None
