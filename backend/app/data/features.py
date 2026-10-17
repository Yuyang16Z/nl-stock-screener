"""指标计算：纯函数，每个指标产出带证据链（Evidence）的 MetricValue。

输入为 repository 组装的 RawBundle（逐股、逐数据集的原始响应或错误）。规则：
- 原始字段 null → missing（“接口返回空值（该期未披露或不适用）”）；字符串无法转数值 → missing；
- 数据集请求失败 → error（原因含错误码与 request_id）；3001/3002 视为数据缺失（missing），3004 视为不适用；
- 金融企业的毛利率、流动比率为 null → not_applicable；market_cap_est 在 ps_ttm≤0 或营收TTM≤0 时 not_applicable；
- 价格窗口样本 < 0.8×N → missing。缺失 / 失败 / 不适用绝不当作通过。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from ..catalog import INDICATOR_FIELDS as CATALOG_INDICATOR_FIELDS
from ..catalog import METRICS
from ..schemas import Evidence, MetricStatus, MetricValue, ReportPeriod, SourceRef, StockFeatures, StockInfo
from .fuyao import FuyaoError, FuyaoResponse, dataset_label
from .periods import date_to_datetime, ms_to_date, ms_to_datetime, parse_date, parse_report, report_label, report_period_end

# ───────────────────────────── 口径常量 ─────────────────────────────

# 利润表季报按 A 股惯例视为“年初至今累计值”（True）；False 时 TTM = 最近四个单季之和。待用真实数据核实。
INCOME_IS_YTD = True
MIN_SAMPLE_RATIO = 0.8  # 价格窗口最少样本比例
TRADING_DAYS_PER_YEAR = 252
DIVIDEND_WINDOW_DAYS = 365
YI = 1e8  # 元 → 亿元
FINANCIAL_INDUSTRY_KEYWORDS = ("银行", "证券", "保险", "多元金融")
FINANCIAL_NAME_KEYWORDS = ("银行", "证券", "保险")  # 行业未知时按证券简称兜底判断
NULL_REASON = "接口返回空值（该期未披露或不适用）"

# 财务指标 index_id 以真实接口为准（成长类带 calculate_ 前缀，净利润同比为归母口径），见 docs/DATA_SOURCES.md §3
INDICATOR_FIELDS: dict[str, str] = dict(CATALOG_INDICATOR_FIELDS)
VALUATION_FIELDS: dict[str, str] = {"pe_ttm": "pe_ttm", "pb_mrq": "pb_mrq", "ps_ttm": "ps_ttm", "pcf_ttm": "pcf_ttm"}
SNAPSHOT_FIELDS: dict[str, str] = {"pct_change_today": "price_change_ratio_pct"}
INCOME_FIELDS: dict[str, str] = {"revenue_amount": "operating_income", "parent_np_amount": "parent_holder_net_profit"}
WINDOW_DAYS: dict[str, int] = {
    "volatility_250d": 250,
    "max_drawdown_250d": 250,
    "beta_250d": 250,
    "return_20d": 20,
    "return_60d": 60,
    "return_250d": 250,
    "avg_amount_20d": 20,
}
# 可用于行业 / 全市场参考组的“廉价直取”指标：metric → (数据集, 字段)
REFERENCE_FIELDS: dict[str, tuple[str, str]] = {
    **{m: ("valuations", f) for m, f in VALUATION_FIELDS.items()},
    **{m: ("snapshot", f) for m, f in SNAPSHOT_FIELDS.items()},
}
FINANCIAL_NA_METRICS = frozenset({"gross_margin", "current_ratio"})
METRIC_NOTES: dict[str, str] = {
    "roe": "报告期累计值（未年化）",
    "roe_deducted": "报告期累计值（未年化）",
    "revenue_amount": "报告期累计值；原始单位为元，已换算为亿元",
    "parent_np_amount": "报告期累计值；原始单位为元，已换算为亿元",
}
_FIELD_UNIT: dict[str, str] = {f: METRICS[m].unit for m, f in INDICATOR_FIELDS.items()}
_STATUS_RANK: dict[str, int] = {"error": 0, "not_applicable": 1, "missing": 2}

# ───────────────────────────── 输入结构 ─────────────────────────────


@dataclass
class Fetched:
    """某股某数据集的取数结果：成功响应（批量接口另带本股记录 row）或错误。"""

    dataset: str
    response: FuyaoResponse | None = None
    error: FuyaoError | None = None
    row: dict[str, Any] | None = None
    _bars: list[Bar] | None = field(default=None, init=False, repr=False, compare=False)  # 日K解析缓存

    @property
    def ok(self) -> bool:
        return self.error is None and self.response is not None


@dataclass
class StockRaw:
    info: StockInfo
    datasets: dict[str, Fetched] = field(default_factory=dict)


@dataclass
class RawBundle:
    stocks: dict[str, StockRaw]
    benchmark: Fetched | None = None  # 沪深300 日K（beta 用）
    trade_date: date | None = None  # 市场最近交易日（用于判断个股价格是否过期）


@dataclass(frozen=True)
class Bar:
    day: date
    close: float | None
    turnover: float | None
    volume: float | None


@dataclass(frozen=True)
class DividendEvent:
    day: date
    cash: float | None  # 每股现金分红（税前，元）
    bonus: float | None  # 每股送股比例


@dataclass
class TTMResult:
    value: float  # 元
    anchor: tuple[int, int]  # (财年, 季度)
    parts: list[tuple[str, tuple[int, int], float]]  # (角色, (年, 季), 值)
    formula: str
    is_ytd: bool


@dataclass
class _In:
    """一个原始输入：状态 + 值 + 证据（派生指标据此组合与传播状态）。"""

    status: MetricStatus
    value: float | None
    evidence: Evidence
    reason: str | None = None
    label: str = ""


# ───────────────────────────── 通用工具 ─────────────────────────────

_NUM_STRIP = re.compile(r"[,\s%]")
_NULL_STRINGS = {"", "-", "--", "none", "null", "nan", "n/a"}


def to_float(value: Any) -> float | None:
    """数值或数值字符串 → float；None / 布尔 / 非有限 / 无法解析 → None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        s = _NUM_STRIP.sub("", value)
        if s.lower() in _NULL_STRINGS:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _clean_zero(v: float, digits: int) -> float:
    return 0.0 if round(v, digits) == 0 else v


def fmt_value(value: float | int | bool | None, unit: str) -> str:
    """展示格式：12.30%、21.36倍、523.4亿元、365天、0.85、是/否；None 或非有限 → "--"。"""
    if value is None:
        return "--"
    if isinstance(value, (bool, np.bool_)):
        return "是" if value else "否"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(v):
        return "--"
    if unit == "%":
        return f"{_clean_zero(v, 2):.2f}%"
    if unit == "倍":
        return f"{_clean_zero(v, 2):.2f}倍"
    if unit == "亿元":
        return f"{_clean_zero(v, 1):.1f}亿元" if abs(v) >= 100 else f"{_clean_zero(v, 2):.2f}亿元"
    if unit == "天":
        return f"{int(round(v))}天"
    if unit == "百分点":
        return f"{_clean_zero(v, 2):+.2f}个百分点"
    if unit == "元":
        if abs(v) >= YI:
            return f"{_clean_zero(v / YI, 2):.2f}亿元"
        if abs(v) >= 1e4:
            return f"{_clean_zero(v / 1e4, 2):.2f}万元"
        return f"{_clean_zero(v, 2):.2f}元"
    if unit == "元/股":
        return f"{_clean_zero(v, 4):.4f}".rstrip("0").rstrip(".") + "元/股"
    if unit == "":
        return f"{_clean_zero(v, 2):.2f}"
    return f"{_clean_zero(v, 2):.2f}{unit}"


def _display(value: float | bool | None, unit: str, status: str) -> str:
    if status == "not_applicable":
        return "不适用"
    if status != "ok":
        return "--"
    return fmt_value(value, unit)


def _resp_time(resp: FuyaoResponse) -> datetime:
    return ms_to_datetime(resp.data_timestamp) if resp.data_timestamp else resp.fetched_at


def _error_status(err: FuyaoError) -> MetricStatus:
    if err.kind in ("not_found", "not_ready"):
        return "missing"
    if err.kind == "unsupported":
        return "not_applicable"
    return "error"


def _merge_sources(inputs: Iterable[_In]) -> list[SourceRef]:
    out: list[SourceRef] = []
    seen: set[tuple[Any, ...]] = set()
    for inp in inputs:
        for s in inp.evidence.sources:
            key = (s.provider, s.endpoint, s.field, s.request_id, tuple(sorted((k, str(v)) for k, v in s.params.items())))
            if key not in seen:
                seen.add(key)
                out.append(s)
    return out


def _worst(inputs: Sequence[_In]) -> tuple[MetricStatus, str] | None:
    """组合输入的状态：error > not_applicable > missing；原因逐项列出。"""
    bad = [i for i in inputs if i.status != "ok"]
    if not bad:
        return None
    status = min((i.status for i in bad), key=lambda s: _STATUS_RANK.get(s, 3))
    reason = "；".join(f"{i.label}：{i.reason}" if i.label else (i.reason or "") for i in bad)
    return status, reason


def is_financial(info: StockInfo) -> tuple[bool, str]:
    """金融企业判断：行业名含 银行/证券/保险/多元金融；行业未知时按简称兜底。返回 (是否, 依据)。"""
    if info.industry:
        hit = next((k for k in FINANCIAL_INDUSTRY_KEYWORDS if k in info.industry), None)
        return hit is not None, f"所属行业“{info.industry}”"
    if info.name:
        hit = next((k for k in FINANCIAL_NAME_KEYWORDS if k in info.name), None)
        if hit:
            return True, f"证券简称含“{hit}”（行业归属未知，按简称判断）"
    return False, ""


# ───────────────────────────── 原始数据解析 ─────────────────────────────


def indicator_values(data: Any) -> dict[str, Any]:
    """财务指标响应 data.abilities[].indicators[] → {index_id: 原始值（字符串或 None）}。"""
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    for ability in data.get("abilities") or []:
        if not isinstance(ability, dict):
            continue
        for ind in ability.get("indicators") or []:
            if isinstance(ind, dict) and ind.get("index_id"):
                out[str(ind["index_id"])] = ind.get("value")
    return out


_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_CN_OFFSET_MS = 8 * 3_600_000
_DAY_MS = 86_400_000


def bar_date(ms: float) -> date:
    """日K毫秒戳 → 上海日期（固定 UTC+8 整数换算；1991 年后中国无夏令时，与 ms_to_date 等价且快得多）。"""
    return date.fromordinal(_EPOCH_ORDINAL + int((ms + _CN_OFFSET_MS) // _DAY_MS))


def parse_bars(data: Any) -> list[Bar]:
    """日K响应 → 按日期升序、去重的 Bar 列表。"""
    items = data.get("item") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    by_day: dict[date, Bar] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        ms = to_float(it.get("date_ms"))
        if ms is None:
            continue
        d = bar_date(ms)
        by_day[d] = Bar(d, to_float(it.get("close_price")), to_float(it.get("turnover")), to_float(it.get("volume")))
    return [by_day[d] for d in sorted(by_day)]


def trading_bars(bars: Iterable[Bar]) -> list[Bar]:
    """剔除收盘价缺失/非正与零成交（成交量或成交额为 0，视为停牌）记录；与回测口径一致。"""
    return [
        b
        for b in bars
        if b.close is not None
        and b.close > 0
        and not (b.volume is not None and b.volume <= 0)
        and not (b.turnover is not None and b.turnover <= 0)
    ]


def market_trade_date(last_dates: Iterable[date | None]) -> date | None:
    """市场最近交易日：各股最近一根有效日K日期的众数（并列取较晚者）。

    用众数而非最大值：个别序列（如盘中先出现当日K线的指数）不会把全部股票误判为过期；停牌股仍被识别。
    """
    counts: dict[date, int] = {}
    for d in last_dates:
        if d is not None:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    return max(d for d, c in counts.items() if c == top)


def bars_from_response(resp: FuyaoResponse | None) -> list[Bar]:
    return trading_bars(parse_bars(resp.data)) if resp is not None else []


def fetched_bars(fetched: Fetched) -> list[Bar]:
    """有效日K（解析结果缓存在 Fetched 上：基准序列被所有股票共享，只解析一次）。"""
    if fetched._bars is None:
        fetched._bars = bars_from_response(fetched.response)
    return fetched._bars


def latest_bar_date(fetched: Fetched | None) -> date | None:
    if fetched is None or not fetched.ok:
        return None
    bars = fetched_bars(fetched)
    return bars[-1].day if bars else None


def dividend_events(data: Any) -> list[DividendEvent]:
    items = data.get("item") if isinstance(data, dict) else None
    out: list[DividendEvent] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        ms = to_float(it.get("ex_date_ms"))
        if ms is None:
            continue
        out.append(DividendEvent(ms_to_date(ms), to_float(it.get("dividend_per_share")), to_float(it.get("per_share_bonus"))))
    return sorted(out, key=lambda e: e.day)


_FISCAL_Q = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4, "H1": 2, "Q9": 3}
_MONTH_Q = {3: 1, 6: 2, 9: 3, 12: 4}


def income_period_key(item: Mapping[str, Any]) -> tuple[int, int] | None:
    """利润表记录 → (财年, 季度)：优先 fiscal_year + fiscal_period，否则按 period_end_ms 月份。"""
    fy = to_float(item.get("fiscal_year"))
    fp = str(item.get("fiscal_period") or "").strip().upper()
    if fy is not None and fp in _FISCAL_Q:
        return int(fy), _FISCAL_Q[fp]
    pe = to_float(item.get("period_end_ms"))
    if pe is not None:
        d = ms_to_date(pe)
        if d.month in _MONTH_Q:
            return d.year, _MONTH_Q[d.month]
    return None


def income_items(data: Any) -> list[dict[str, Any]]:
    """利润表 item[] → 带 _key=(年, 季) 的记录，按报告期降序；同一报告期保留首条。"""
    items = data.get("item") if isinstance(data, dict) else None
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        key = income_period_key(it)
        if key is not None and key not in out:
            out[key] = {**it, "_key": key}
    return [out[k] for k in sorted(out, reverse=True)]


def find_income(items: Sequence[Mapping[str, Any]], year: int, quarter: int) -> Mapping[str, Any] | None:
    return next((it for it in items if it.get("_key") == (year, quarter)), None)


def _prev_quarter(key: tuple[int, int]) -> tuple[int, int]:
    y, q = key
    return (y, q - 1) if q > 1 else (y - 1, 4)


def _disclosed(item: Mapping[str, Any], as_of: date | None) -> bool:
    if as_of is None:
        return True
    rd = to_float(item.get("report_date_ms"))
    return rd is None or ms_to_date(rd) <= as_of


def revenue_ttm(
    items: Sequence[Mapping[str, Any]],
    *,
    as_of: date | None = None,
    is_ytd: bool | None = None,
    field_name: str = "operating_income",
) -> TTMResult | str:
    """营业收入 TTM（元）。返回 TTMResult，或无法计算时的原因字符串。

    锚定为最近一期已披露（report_date ≤ as_of）且营收非空的报告期，与估值快照 TTM 口径对齐。
    YTD 口径：本期累计 + 上年年报 − 上年同期累计（锚定年报时即年报值）；单季口径：最近四个单季之和。
    """
    ytd = INCOME_IS_YTD if is_ytd is None else is_ytd
    avail = [it for it in items if to_float(it.get(field_name)) is not None and _disclosed(it, as_of) and it.get("_key")]
    if not avail:
        return "利润表无可用的营业收入数据"
    anchor = max(avail, key=lambda it: it["_key"])
    y, q = anchor["_key"]
    cur = float(to_float(anchor[field_name]) or 0.0)
    if ytd:
        if q == 4:
            return TTMResult(cur, (y, 4), [("最近年报", (y, 4), cur)], "营业收入TTM = 最近年报营业收入", True)
        fy = find_income(items, y - 1, 4)
        same = find_income(items, y - 1, q)
        fy_v = to_float(fy.get(field_name)) if fy else None
        same_v = to_float(same.get(field_name)) if same else None
        lacking = [name for name, v in (("上年年报", fy_v), ("上年同期", same_v)) if v is None]
        if lacking or fy_v is None or same_v is None:
            return f"缺少{'、'.join(lacking)}营业收入，无法计算营业收入TTM"
        return TTMResult(
            cur + fy_v - same_v,
            (y, q),
            [("本期累计", (y, q), cur), ("上年年报", (y - 1, 4), fy_v), ("上年同期累计", (y - 1, q), same_v)],
            "营业收入TTM = 本期累计 + 上年年报 − 上年同期累计",
            True,
        )
    keys = [(y, q)]
    for _ in range(3):
        keys.append(_prev_quarter(keys[-1]))
    parts: list[tuple[str, tuple[int, int], float]] = []
    for k in keys:
        it = find_income(items, *k)
        v = to_float(it.get(field_name)) if it else None
        if v is None:
            return f"缺少 {k[0]} 年第 {k[1]} 季度单季营业收入，无法计算营业收入TTM"
        parts.append(("单季", k, v))
    return TTMResult(sum(p[2] for p in parts), (y, q), parts, "营业收入TTM = 最近四个单季营业收入之和", False)


def reference_value(metric: str, row: Mapping[str, Any] | None) -> float | None:
    """参考组取值：只取直取字段且有限的数值（正值过滤交给引擎）。"""
    if row is None or metric not in REFERENCE_FIELDS:
        return None
    return to_float(row.get(REFERENCE_FIELDS[metric][1]))


# ───────────────────────────── 纯数值计算（回测可复用） ─────────────────────────────


def calc_volatility(closes: Sequence[float]) -> float | None:
    """年化波动率（%）：std(ln(Pt/Pt−1), 样本标准差) × √252 × 100。"""
    a = np.asarray(closes, dtype=float)
    if a.size < 3 or np.any(a <= 0) or not np.all(np.isfinite(a)):
        return None
    r = np.diff(np.log(a))
    return float(np.std(r, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)


def calc_max_drawdown(closes: Sequence[float]) -> tuple[float, int, int] | None:
    """最大回撤（%，正数）及 (峰值下标, 谷值下标)。"""
    a = np.asarray(closes, dtype=float)
    if a.size < 2 or np.any(a <= 0) or not np.all(np.isfinite(a)):
        return None
    runmax = np.maximum.accumulate(a)
    dd = 1.0 - a / runmax
    trough = int(np.argmax(dd))
    peak = int(np.argmax(a[: trough + 1]))
    return float(dd[trough] * 100.0), peak, trough


def calc_return(closes: Sequence[float]) -> float | None:
    """区间涨跌幅（%）：(P_last ÷ P_first − 1) × 100。"""
    if len(closes) < 2 or closes[0] is None or closes[-1] is None or closes[0] <= 0:
        return None
    return (float(closes[-1]) / float(closes[0]) - 1.0) * 100.0


def calc_beta(stock_returns: Sequence[float], market_returns: Sequence[float]) -> float | None:
    """beta = cov(ri, rm) ÷ var(rm)（同为样本口径）；基准方差为 0 时返回 None。"""
    ri = np.asarray(stock_returns, dtype=float)
    rm = np.asarray(market_returns, dtype=float)
    if ri.size != rm.size or ri.size < 3:
        return None
    var = float(np.var(rm, ddof=1))
    if not math.isfinite(var) or var <= 0:
        return None
    cov = float(np.cov(ri, rm, ddof=1)[0, 1])
    return cov / var


def simple_returns(prices: Sequence[float]) -> list[float]:
    return [float(prices[i]) / float(prices[i - 1]) - 1.0 for i in range(1, len(prices))]


def _window_period(n: int, k: int, last: date | None) -> str:
    if last is None:
        return f"近{n}个交易日"
    if k >= n:
        return f"近{n}个交易日（截至{last.isoformat()}）"
    return f"近{k}个交易日（不足{n}个，截至{last.isoformat()}）"


def _raw_ev(
    name: str,
    value: float | bool | str | None,
    *,
    unit: str,
    period: str | None,
    as_of: datetime | None = None,
    sources: Sequence[SourceRef] = (),
    display: str | None = None,
    note: str | None = None,
) -> Evidence:
    return Evidence(
        metric=name,
        value=value,
        unit=unit,
        display=display if display is not None else (fmt_value(value, unit) if not isinstance(value, str) else value),
        period=period,
        as_of=as_of,
        sources=list(sources),
        note=note,
    )


def _parse_value(
    container: Mapping[str, Any],
    key: str,
    *,
    unit: str,
    period: str | None,
    as_of: datetime | None,
    src: SourceRef,
    label: str,
) -> _In:
    def ev(v: float | None) -> Evidence:
        return _raw_ev(key, v, unit=unit, period=period, as_of=as_of, sources=[src])

    if key not in container:
        return _In("missing", None, ev(None), f"接口未返回字段 {key}", label)
    raw = container[key]
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in _NULL_STRINGS):
        return _In("missing", None, ev(None), NULL_REASON, label)
    v = to_float(raw)
    if v is None:
        return _In("missing", None, ev(None), f"接口返回值无法解析为数值（{raw!r}）", label)
    return _In("ok", v, ev(v), None, label)


# ───────────────────────────── 单股计算 ─────────────────────────────


class _Calc:
    def __init__(
        self,
        stock: StockRaw,
        report: ReportPeriod,
        as_of: date,
        benchmark: Fetched | None,
        trade_date: date | None,
    ) -> None:
        self.stock = stock
        self.info = stock.info
        self.code = stock.info.thscode
        self.report = report
        self.as_of = as_of
        self.benchmark = benchmark
        self.trade_date = trade_date
        self._memo: dict[str, MetricValue] = {}
        self._cache: dict[str, Any] = {}
        self._handlers: dict[str, Callable[[], MetricValue]] = {
            "roe_annualized": self._roe_annualized,
            "roe_change": lambda: self._yoy_change("roe_change", "index_weighted_avg_roe", "ROE", None),
            "gross_margin_change": lambda: self._yoy_change(
                "gross_margin_change", "sale_gross_margin", "销售毛利率", "gross_margin"
            ),
            "volatility_250d": self._volatility,
            "max_drawdown_250d": self._max_drawdown,
            "beta_250d": self._beta,
            "return_20d": lambda: self._period_return("return_20d"),
            "return_60d": lambda: self._period_return("return_60d"),
            "return_250d": lambda: self._period_return("return_250d"),
            "avg_amount_20d": self._avg_amount,
            "market_cap_est": self._market_cap,
            "dividend_yield_ttm": self._dividend_yield,
            "is_st": self._is_st,
            "listing_days": self._listing_days,
        }

    # ── 调度 ──

    def metric(self, mid: str) -> MetricValue:
        if mid not in self._memo:
            self._memo[mid] = self._compute(mid)
        return self._memo[mid]

    def _compute(self, mid: str) -> MetricValue:
        if mid in VALUATION_FIELDS:
            return self._direct(mid, self._row_input("valuations", VALUATION_FIELDS[mid], "倍"))
        if mid in SNAPSHOT_FIELDS:
            return self._direct(mid, self._row_input("snapshot", SNAPSHOT_FIELDS[mid], "%"))
        if mid in INDICATOR_FIELDS:
            inp = self._ind_input(INDICATOR_FIELDS[mid], yoy=False, label=METRICS[mid].name)
            if mid in FINANCIAL_NA_METRICS:
                inp = self._financial_na(mid, inp)
            return self._direct(mid, inp)
        if mid in INCOME_FIELDS:
            return self._income_amount(mid)
        handler = self._handlers.get(mid)
        if handler is None:
            return MetricValue(metric=mid, status="error", reason=f"未实现的指标：{mid}")
        return handler()

    # ── 输出构造 ──

    def _direct(self, mid: str, inp: _In, *, scale: float = 1.0) -> MetricValue:
        md = METRICS[mid]
        value = inp.value * scale if inp.status == "ok" and inp.value is not None else None
        ev = Evidence(
            metric=mid,
            value=value,
            unit=md.unit,
            display=_display(value, md.unit, inp.status),
            period=inp.evidence.period,
            as_of=inp.evidence.as_of,
            sources=list(inp.evidence.sources),
            derived=False,
            inputs=[inp.evidence] if scale != 1.0 else [],
            note=METRIC_NOTES.get(mid),
        )
        if inp.status != "ok":
            return MetricValue(metric=mid, status=inp.status, evidence=ev, reason=inp.reason)
        return MetricValue(metric=mid, status="ok", value=value, evidence=ev)

    def _derived_bad(
        self, mid: str, status: MetricStatus, reason: str, *, period: str | None, inputs: Sequence[_In], as_of: datetime | None = None, note: str | None = None
    ) -> MetricValue:
        md = METRICS[mid]
        ev = Evidence(
            metric=mid,
            value=None,
            unit=md.unit,
            display=_display(None, md.unit, status),
            period=period,
            as_of=as_of,
            sources=_merge_sources(inputs),
            derived=True,
            formula=md.formula,
            inputs=[i.evidence for i in inputs],
            note=note,
        )
        return MetricValue(metric=mid, status=status, evidence=ev, reason=reason)

    def _derived_ok(
        self,
        mid: str,
        value: float,
        *,
        period: str | None,
        inputs: Sequence[_In],
        formula: str | None = None,
        as_of: datetime | None = None,
        note: str | None = None,
        sources: Sequence[SourceRef] | None = None,
        extra_inputs: Sequence[Evidence] = (),
        stale: bool = False,
    ) -> MetricValue:
        md = METRICS[mid]
        ev = Evidence(
            metric=mid,
            value=value,
            unit=md.unit,
            display=fmt_value(value, md.unit),
            period=period,
            as_of=as_of,
            sources=list(sources) if sources is not None else _merge_sources(inputs),
            derived=True,
            formula=formula or md.formula,
            inputs=[i.evidence for i in inputs] + list(extra_inputs),
            note=note,
        )
        return MetricValue(metric=mid, status="ok", value=value, evidence=ev, stale=stale)

    # ── 原始输入 ──

    def _fetched(self, ds: str) -> Fetched | None:
        return self.stock.datasets.get(ds)

    def _dataset_bad(self, ds: str, name: str, unit: str, period: str | None, label: str) -> _In | None:
        f = self._fetched(ds)
        if f is None or (f.error is None and f.response is None):
            ev = _raw_ev(name, None, unit=unit, period=period)
            return _In("missing", None, ev, f"未取得数据集：{dataset_label(ds)}", label)
        if f.error is not None:
            ev = _raw_ev(name, None, unit=unit, period=period, sources=[f.error.source(thscode=self.code)])
            return _In(_error_status(f.error), None, ev, f.error.describe(), label)
        return None

    def _row_input(self, ds: str, name: str, unit: str, label: str | None = None) -> _In:
        label = label or name
        bad = self._dataset_bad(ds, name, unit, "实时快照", label)
        if bad is not None:
            return bad
        f = self._fetched(ds)
        assert f is not None and f.response is not None
        resp = f.response
        t_api = _resp_time(resp)
        if self.trade_date is not None:
            # 实测：快照/估值信封 data.timestamp 为服务端就绪时间（周日也显示当前时刻），不是成交时间
            t = datetime.combine(self.trade_date, datetime.min.time(), tzinfo=t_api.tzinfo).replace(hour=15)
            period = f"最近交易日 {self.trade_date.isoformat()} 收盘后快照（接口返回时间 {t_api:%Y-%m-%d %H:%M}）"
        else:
            t = t_api
            period = f"实时快照（接口返回时间 {t_api:%Y-%m-%d %H:%M}）"
        src = resp.source(name, thscode=self.code)
        if f.row is None:
            ev = _raw_ev(name, None, unit=unit, period=period, as_of=t, sources=[src])
            return _In("missing", None, ev, "批量接口未返回该股票记录", label)
        return _parse_value(f.row, name, unit=unit, period=period, as_of=t, src=src, label=label)

    def _ind_input(self, name: str, *, yoy: bool, label: str) -> _In:
        ds = "indicators_yoy" if yoy else "indicators"
        rep = self.report.yoy_report if yoy else self.report.report
        period = f"{report_label(rep)}（{rep}）"
        unit = _FIELD_UNIT.get(name, "%")
        bad = self._dataset_bad(ds, name, unit, period, label)
        if bad is not None:
            return bad
        f = self._fetched(ds)
        assert f is not None and f.response is not None
        key = f"ind:{ds}"
        if key not in self._cache:
            self._cache[key] = indicator_values(f.response.data)
        values: dict[str, Any] = self._cache[key]
        as_of = date_to_datetime(report_period_end(rep))
        return _parse_value(values, name, unit=unit, period=period, as_of=as_of, src=f.response.source(name), label=label)

    def _financial_na(self, mid: str, inp: _In) -> _In:
        if inp.status == "missing" and inp.reason == NULL_REASON:
            fin, basis = is_financial(self.info)
            if fin:
                reason = f"金融企业（{basis}）通常无{METRICS[mid].name}，接口返回空值，判为不适用"
                return _In("not_applicable", None, inp.evidence, reason, inp.label)
        return inp

    def _report_text(self) -> str:
        return f"{self.report.label}（{self.report.report}）"

    def _report_as_of(self) -> datetime:
        return date_to_datetime(report_period_end(self.report.report))

    # ── 财务指标派生 ──

    def _roe_annualized(self) -> MetricValue:
        mid = "roe_annualized"
        roe = self.metric("roe")
        q = self.report.quarters
        roe_value = to_float(roe.value) if roe.status == "ok" else None
        roe_ev = roe.evidence or _raw_ev("roe", None, unit="%", period=self._report_text())
        roe_in = _In(roe.status, roe_value, roe_ev, roe.reason, "ROE（报告期累计）")
        note = "年化估算（线性年化，忽略季节性）"
        if roe.status != "ok" or roe_in.value is None:
            return self._derived_bad(mid, roe.status, roe.reason or "", period=self._report_text(), inputs=[roe_in], as_of=self._report_as_of(), note=note)
        v = roe_in.value * 4.0 / q
        formula = f"{METRICS[mid].formula} = {roe_in.value:g} × 4 ÷ {q}"
        return self._derived_ok(mid, v, period=self._report_text(), inputs=[roe_in], formula=formula, as_of=self._report_as_of(), note=note)

    def _yoy_change(self, mid: str, name: str, label: str, na_metric: str | None) -> MetricValue:
        cur = self._ind_input(name, yoy=False, label=f"本期{label}")
        prev = self._ind_input(name, yoy=True, label=f"上年同期{label}")
        if na_metric:
            cur, prev = self._financial_na(na_metric, cur), self._financial_na(na_metric, prev)
        yoy_label = report_label(self.report.yoy_report)
        period = f"{self.report.label} 对比 {yoy_label}（{self.report.report} vs {self.report.yoy_report}）"
        bad = _worst([cur, prev])
        if bad is not None:
            return self._derived_bad(mid, bad[0], bad[1], period=period, inputs=[cur, prev], as_of=self._report_as_of())
        assert cur.value is not None and prev.value is not None
        v = cur.value - prev.value
        formula = f"{METRICS[mid].formula} = {cur.value:g} − {prev.value:g}"
        return self._derived_ok(mid, v, period=period, inputs=[cur, prev], formula=formula, as_of=self._report_as_of())

    # ── 利润表 ──

    def _income_data(self) -> tuple[list[dict[str, Any]], FuyaoResponse] | _In:
        bad = self._dataset_bad("income", "operating_income", "元", self._report_text(), "利润表")
        if bad is not None:
            return bad
        f = self._fetched("income")
        assert f is not None and f.response is not None
        if "income" not in self._cache:
            self._cache["income"] = income_items(f.response.data)
        return self._cache["income"], f.response

    def _income_amount(self, mid: str) -> MetricValue:
        name = INCOME_FIELDS[mid]
        r = self._income_data()
        if isinstance(r, _In):
            r.label = METRICS[mid].name
            return self._direct(mid, r, scale=1.0 / YI)
        items, resp = r
        y, q = parse_report(self.report.report)
        src = resp.source(name)
        it = find_income(items, y, q)
        if it is None:
            ev = _raw_ev(name, None, unit="元", period=self._report_text(), as_of=self._report_as_of(), sources=[src])
            inp = _In("missing", None, ev, f"利润表未包含{self.report.label}（可能尚未披露）", METRICS[mid].name)
        else:
            inp = _parse_value(it, name, unit="元", period=self._report_text(), as_of=self._report_as_of(), src=src, label=METRICS[mid].name)
        return self._direct(mid, inp, scale=1.0 / YI)

    def _revenue_ttm_input(self) -> _In:
        label = "营业收入TTM"
        r = self._income_data()
        if isinstance(r, _In):
            r.label = label
            return r
        items, resp = r
        src = resp.source("operating_income")
        res = revenue_ttm(items, as_of=self.as_of)
        if isinstance(res, str):
            ev = _raw_ev("revenue_ttm", None, unit="亿元", period="近四个季度", sources=[src])
            return _In("missing", None, ev, res, label)
        anchor = f"{res.anchor[0]}-{res.anchor[1]}"
        parts = [
            _raw_ev(
                f"operating_income（{role}）",
                v / YI,
                unit="亿元",
                period=f"{report_label(f'{k[0]}-{k[1]}')}（{k[0]}-{k[1]}）",
                as_of=date_to_datetime(report_period_end(f"{k[0]}-{k[1]}")),
                sources=[src],
            )
            for role, k, v in res.parts
        ]
        note = "利润表季报为年初至今累计值（已用真实接口核实：Q4 累计值等于年报）" if res.is_ytd else "利润表季报按单季值处理"
        ev = Evidence(
            metric="revenue_ttm",
            value=res.value / YI,
            unit="亿元",
            display=fmt_value(res.value / YI, "亿元"),
            period=f"截至{report_label(anchor)}的近四个季度",
            as_of=date_to_datetime(report_period_end(anchor)),
            sources=[src],
            derived=True,
            formula=res.formula,
            inputs=parts,
            note=note,
        )
        return _In("ok", res.value, ev, None, label)

    def _ttm_input(self, field_name: str, label: str, key: str) -> _In:
        """利润表字段 TTM（元）：本期累计 + 上年年报 − 上年同期累计。"""
        r = self._income_data()
        if isinstance(r, _In):
            r.label = label
            return r
        items, resp = r
        src = resp.source(field_name)
        res = revenue_ttm(items, as_of=self.as_of, field_name=field_name)
        if isinstance(res, str):
            ev = _raw_ev(key, None, unit="亿元", period="近四个季度", sources=[src])
            return _In("missing", None, ev, res.replace("营业收入", label.replace("TTM", "")), label)
        anchor = f"{res.anchor[0]}-{res.anchor[1]}"
        parts = [
            _raw_ev(
                f"{field_name}（{role}）",
                v / YI,
                unit="亿元",
                period=f"{report_label(f'{k[0]}-{k[1]}')}（{k[0]}-{k[1]}）",
                as_of=date_to_datetime(report_period_end(f"{k[0]}-{k[1]}")),
                sources=[src],
            )
            for role, k, v in res.parts
        ]
        ev = Evidence(
            metric=key,
            value=res.value / YI,
            unit="亿元",
            display=fmt_value(res.value / YI, "亿元"),
            period=f"截至{report_label(anchor)}的近四个季度",
            as_of=date_to_datetime(report_period_end(anchor)),
            sources=[src],
            derived=True,
            formula=res.formula.replace("营业收入", label.replace("TTM", "")),
            inputs=parts,
            note="利润表季报为年初至今累计值（已用真实接口核实）",
        )
        return _In("ok", res.value, ev, None, label)

    def _market_cap(self) -> MetricValue:
        """估算总市值：优先 PE(TTM) × 归母净利润TTM；亏损或缺失时用 PS(TTM) × 营业收入TTM。

        实测（2026-10-17）：茅台 PE 法 16613.6 亿、iFinD 总市值 1.6614 万亿；PS 法 16317.6 亿（偏低 1.8%）。
        """
        mid = "market_cap_est"
        note = "估算值：数据源暂未提供总市值字段"
        pe = self._row_input("valuations", "pe_ttm", "倍", label="市盈率TTM")
        np_ttm = self._ttm_input("parent_holder_net_profit", "归母净利润TTM", "parent_np_ttm")
        if pe.status == "ok" and np_ttm.status == "ok" and pe.value is not None and np_ttm.value is not None and pe.value > 0 and np_ttm.value > 0:
            v = pe.value * np_ttm.value / YI
            formula = f"pe_ttm × 归母净利润TTM ÷ 1e8 = {pe.value:g} × {np_ttm.value / YI:.2f}亿元"
            return self._derived_ok(mid, v, period="估值快照 × 归母净利润TTM", inputs=[pe, np_ttm], formula=formula, as_of=pe.evidence.as_of, note=note)
        ps = self._row_input("valuations", "ps_ttm", "倍", label="市销率TTM")
        ttm = self._revenue_ttm_input()
        period = "估值快照 × 营业收入TTM（PE 不可用，改用市销率法）"
        inputs = [pe, np_ttm, ps, ttm]
        bad = _worst([ps, ttm])
        if bad is not None:
            return self._derived_bad(mid, bad[0], bad[1], period=period, inputs=inputs, as_of=ps.evidence.as_of, note=note)
        assert ps.value is not None and ttm.value is not None
        if ps.value <= 0 or ttm.value <= 0:
            reason = (
                f"市盈率法不可用，且市销率TTM（{fmt_value(ps.value, '倍')}）或营业收入TTM（{fmt_value(ttm.value / YI, '亿元')}）非正，"
                "无法估算市值"
            )
            return self._derived_bad(mid, "not_applicable", reason, period=period, inputs=inputs, as_of=ps.evidence.as_of, note=note)
        v = ps.value * ttm.value / YI
        formula = f"ps_ttm × 营业收入TTM ÷ 1e8 = {ps.value:g} × {ttm.value / YI:.2f}亿元（市盈率非正或归母净利润TTM缺失，改用市销率法）"
        return self._derived_ok(mid, v, period=period, inputs=inputs, formula=formula, as_of=ps.evidence.as_of, note=note)

    # ── 分红 ──

    def _dividends_input(self) -> _In:
        label = f"近{DIVIDEND_WINDOW_DAYS}天每股现金分红"
        start = self.as_of - timedelta(days=DIVIDEND_WINDOW_DAYS)
        period = f"近{DIVIDEND_WINDOW_DAYS}天（{start.isoformat()} 至 {self.as_of.isoformat()}，按除权除息日）"
        bad = self._dataset_bad("dividends", "dividend_per_share", "元/股", period, label)
        if bad is not None:
            return bad
        f = self._fetched("dividends")
        assert f is not None and f.response is not None
        src = f.response.source("dividend_per_share")
        events = [e for e in dividend_events(f.response.data) if start < e.day <= self.as_of]
        cash_events = [e for e in events if e.cash is not None]
        total = float(sum(e.cash for e in cash_events if e.cash is not None))
        inputs = [
            _raw_ev("dividend_per_share", e.cash, unit="元/股", period=f"除权除息日 {e.day.isoformat()}", as_of=date_to_datetime(e.day), sources=[src])
            for e in cash_events
        ]
        note = None if any((e.cash or 0) > 0 for e in cash_events) else "近365天无现金分红事件"
        ev = Evidence(
            metric="dividend_per_share_ttm",
            value=total,
            unit="元/股",
            display=fmt_value(total, "元/股"),
            period=period,
            as_of=date_to_datetime(self.as_of),
            sources=[src],
            derived=True,
            formula="Σ 近365天 dividend_per_share（税前）",
            inputs=inputs,
            note=note,
        )
        return _In("ok", total, ev, None, label)

    def _dividend_yield(self) -> MetricValue:
        mid = "dividend_yield_ttm"
        div = self._dividends_input()
        price = self._row_input("snapshot", "last_price", "元", label="最新价")
        period = div.evidence.period
        note = "估算：以除权除息日统计近365天现金分红，送转不计入"
        bad = _worst([div, price])
        if bad is not None:
            return self._derived_bad(mid, bad[0], bad[1], period=period, inputs=[div, price], as_of=price.evidence.as_of, note=note)
        assert div.value is not None and price.value is not None
        if price.value <= 0:
            return self._derived_bad(mid, "missing", "最新价非正，无法计算股息率", period=period, inputs=[div, price], as_of=price.evidence.as_of, note=note)
        v = div.value / price.value * 100.0
        formula = f"Σ 近365天 dividend_per_share ÷ last_price × 100 = {div.value:g} ÷ {price.value:g} × 100"
        return self._derived_ok(mid, v, period=period, inputs=[div, price], formula=formula, as_of=price.evidence.as_of, note=note)

    # ── 价格窗口 ──

    def _bars(self) -> list[Bar] | _In:
        if "bars" not in self._cache:
            bad = self._dataset_bad("history", "close_price", "元", "历史日K（后复权）", "后复权日K")
            if bad is not None:
                self._cache["bars"] = bad
            else:
                f = self._fetched("history")
                assert f is not None
                self._cache["bars"] = fetched_bars(f)
        return self._cache["bars"]

    def _bench_bars(self) -> list[Bar] | _In:
        if "bench" not in self._cache:
            f = self.benchmark
            name, label = "close_price", "沪深300日K"
            if f is None or (f.error is None and f.response is None):
                self._cache["bench"] = _In("missing", None, _raw_ev(name, None, unit="点", period=None), "未取得基准指数（000300.SH）行情", label)
            elif f.error is not None:
                ev = _raw_ev(name, None, unit="点", period=None, sources=[f.error.source()])
                self._cache["bench"] = _In(_error_status(f.error), None, ev, f.error.describe(), label)
            else:
                self._cache["bench"] = fetched_bars(f)
        return self._cache["bench"]

    def _hist_source(self, name: str) -> SourceRef:
        f = self._fetched("history")
        assert f is not None and f.response is not None
        return f.response.source(name)

    def _window(self, mid: str, n: int, *, with_base: bool = True) -> tuple[list[Bar], str, MetricValue | None]:
        """取窗口：with_base 时取最近 n+1 根（含窗口前一日作基准），样本数按区间数计。"""
        r = self._bars()
        if isinstance(r, _In):
            return [], f"近{n}个交易日", self._derived_bad(mid, r.status, f"{r.label}：{r.reason}", period=f"近{n}个交易日", inputs=[r])
        w = r[-(n + 1) :] if with_base else r[-n:]
        k = max(0, len(w) - 1) if with_base else len(w)
        last = w[-1].day if w else None
        period = _window_period(n, k, last)
        need = math.ceil(MIN_SAMPLE_RATIO * n)
        if k < need:
            sample = _raw_ev("sample_size", float(k), unit="个", period=period, display=f"{k}个交易日")
            reason = f"价格样本不足：可用 {k} 个交易日，少于 {n}×{MIN_SAMPLE_RATIO:g}={need}"
            sources = [self._hist_source("close_price")]
            md = METRICS[mid]
            ev = Evidence(metric=mid, value=None, unit=md.unit, display="--", period=period, sources=sources, derived=True, formula=md.formula, inputs=[sample])
            return w, period, MetricValue(metric=mid, status="missing", evidence=ev, reason=reason)
        return w, period, None

    def _stale(self, last: date) -> tuple[bool, str | None]:
        if self.trade_date is not None and last < self.trade_date:
            return True, f"该股最近交易日为 {last.isoformat()}，早于市场最近交易日 {self.trade_date.isoformat()}（可能停牌）"
        return False, None

    def _window_ok(
        self, mid: str, value: float, w: list[Bar], period: str, extra: Sequence[Evidence], *, field_name: str = "close_price", sources: Sequence[SourceRef] | None = None, formula: str | None = None
    ) -> MetricValue:
        last = w[-1].day
        stale, note = self._stale(last)
        return self._derived_ok(
            mid,
            value,
            period=period,
            inputs=[],
            extra_inputs=extra,
            formula=formula,
            as_of=date_to_datetime(last),
            note=note,
            sources=sources if sources is not None else [self._hist_source(field_name)],
            stale=stale,
        )

    def _close_ev(self, b: Bar, role: str) -> Evidence:
        return _raw_ev(
            f"close_price（{role}）",
            b.close,
            unit="元",
            period=b.day.isoformat(),
            as_of=date_to_datetime(b.day),
            sources=[self._hist_source("close_price")],
            note="后复权收盘价",
        )

    def _sample_ev(self, k: int, what: str, period: str) -> Evidence:
        return _raw_ev("sample_size", float(k), unit="个", period=period, display=f"{k}个{what}")

    def _volatility(self) -> MetricValue:
        mid, n = "volatility_250d", WINDOW_DAYS["volatility_250d"]
        w, period, fail = self._window(mid, n)
        if fail is not None:
            return fail
        v = calc_volatility([b.close for b in w if b.close is not None])
        if v is None:
            return self._derived_bad(mid, "missing", "价格序列无效，无法计算波动率", period=period, inputs=[])
        return self._window_ok(mid, v, w, period, [self._sample_ev(len(w) - 1, "日对数收益率（样本标准差）", period)])

    def _max_drawdown(self) -> MetricValue:
        mid, n = "max_drawdown_250d", WINDOW_DAYS["max_drawdown_250d"]
        w, period, fail = self._window(mid, n)
        if fail is not None:
            return fail
        res = calc_max_drawdown([b.close for b in w if b.close is not None])
        if res is None:
            return self._derived_bad(mid, "missing", "价格序列无效，无法计算最大回撤", period=period, inputs=[])
        v, peak, trough = res
        extra = [self._sample_ev(len(w), "后复权收盘价", period)]
        if v > 0:
            extra += [self._close_ev(w[peak], "前高"), self._close_ev(w[trough], "回撤低点")]
        return self._window_ok(mid, v, w, period, extra)

    def _period_return(self, mid: str) -> MetricValue:
        n = WINDOW_DAYS[mid]
        w, period, fail = self._window(mid, n)
        if fail is not None:
            return fail
        v = calc_return([b.close for b in w if b.close is not None])
        if v is None:
            return self._derived_bad(mid, "missing", "价格序列无效，无法计算区间涨跌幅", period=period, inputs=[])
        extra = [self._close_ev(w[0], "区间起点前一日"), self._close_ev(w[-1], "最新")]
        return self._window_ok(mid, v, w, period, extra)

    def _avg_amount(self) -> MetricValue:
        mid, n = "avg_amount_20d", WINDOW_DAYS["avg_amount_20d"]
        w, period, fail = self._window(mid, n, with_base=False)
        if fail is not None:
            return fail
        vals = [b.turnover for b in w if b.turnover is not None]
        need = math.ceil(MIN_SAMPLE_RATIO * n)
        if len(vals) < need:
            reason = f"成交额样本不足：可用 {len(vals)} 个交易日，少于 {n}×{MIN_SAMPLE_RATIO:g}={need}"
            return self._derived_bad(mid, "missing", reason, period=period, inputs=[])
        v = float(np.mean(vals)) / YI
        return self._window_ok(mid, v, w, period, [self._sample_ev(len(vals), "交易日成交额", period)], field_name="turnover")

    def _beta(self) -> MetricValue:
        mid, n = "beta_250d", WINDOW_DAYS["beta_250d"]
        w, period, fail = self._window(mid, n)
        if fail is not None:
            return fail
        bench = self._bench_bars()
        if isinstance(bench, _In):
            return self._derived_bad(mid, bench.status, f"{bench.label}：{bench.reason}", period=period, inputs=[bench])
        bmap = {b.day: b.close for b in bench if b.close is not None}
        pairs = [(b.day, b.close, bmap[b.day]) for b in w if b.close is not None and b.day in bmap]
        k = len(pairs) - 1
        need = math.ceil(MIN_SAMPLE_RATIO * n)
        if k < need:
            reason = f"个股与基准按日期对齐后样本不足：{max(k, 0)} 个，少于 {n}×{MIN_SAMPLE_RATIO:g}={need}"
            return self._derived_bad(mid, "missing", reason, period=period, inputs=[])
        ri = simple_returns([p[1] for p in pairs])
        rm = simple_returns([p[2] for p in pairs])
        v = calc_beta(ri, rm)
        if v is None:
            return self._derived_bad(mid, "missing", "基准日收益方差为 0，无法计算 Beta", period=period, inputs=[])
        assert self.benchmark is not None and self.benchmark.response is not None
        sources = [self._hist_source("close_price"), self.benchmark.response.source("close_price")]
        return self._window_ok(mid, v, w, period, [self._sample_ev(k, "按日期对齐的日收益率", period)], sources=sources)

    # ── 代码表 ──

    def _ticker_row(self, mid: str, field_name: str) -> tuple[Mapping[str, Any], FuyaoResponse] | MetricValue:
        md = METRICS[mid]
        period = f"截至{self.as_of.isoformat()}"
        bad = self._dataset_bad("tickers", field_name, "", period, "代码表")
        if bad is not None:
            ev = Evidence(metric=mid, value=None, unit=md.unit, display="--", period=period, sources=bad.evidence.sources)
            return MetricValue(metric=mid, status=bad.status, evidence=ev, reason=bad.reason)
        f = self._fetched("tickers")
        assert f is not None and f.response is not None
        if f.row is None:
            ev = Evidence(metric=mid, value=None, unit=md.unit, display="--", period=period, sources=[f.response.source(field_name)])
            return MetricValue(metric=mid, status="missing", evidence=ev, reason="A股代码表中未找到该股票")
        return f.row, f.response

    def _is_st(self) -> MetricValue:
        mid = "is_st"
        r = self._ticker_row(mid, "name")
        if isinstance(r, MetricValue):
            return r
        row, resp = r
        period = f"截至{self.as_of.isoformat()}"
        src = resp.source("name")
        name = row.get("name")
        if not name:
            ev = Evidence(metric=mid, value=None, unit="", display="--", period=period, sources=[src])
            return MetricValue(metric=mid, status="missing", evidence=ev, reason="代码表中该股票名称为空")
        v = "ST" in str(name).upper()
        name_ev = _raw_ev("name", str(name), unit="", period=period, as_of=_resp_time(resp), sources=[src])
        ev = Evidence(
            metric=mid,
            value=v,
            unit="",
            display=fmt_value(v, ""),
            period=period,
            as_of=_resp_time(resp),
            sources=[src],
            inputs=[name_ev],
            note="按当前证券简称判断（含 *ST）",
        )
        return MetricValue(metric=mid, status="ok", value=v, evidence=ev)

    def _listing_days(self) -> MetricValue:
        mid = "listing_days"
        md = METRICS[mid]
        r = self._ticker_row(mid, "list_date")
        if isinstance(r, MetricValue):
            return r
        row, resp = r
        period = f"截至{self.as_of.isoformat()}"
        src = resp.source("list_date")
        raw = row.get("list_date")
        d = parse_date(raw) if raw else None
        if d is None:
            reason = NULL_REASON if not raw else f"上市日期无法解析（{raw!r}）"
            ev = Evidence(metric=mid, value=None, unit=md.unit, display="--", period=period, sources=[src], derived=True, formula=md.formula)
            return MetricValue(metric=mid, status="missing", evidence=ev, reason=reason)
        days = (self.as_of - d).days
        if days < 0:
            ev = Evidence(metric=mid, value=None, unit=md.unit, display="--", period=period, sources=[src], derived=True, formula=md.formula)
            return MetricValue(metric=mid, status="missing", evidence=ev, reason=f"上市日期 {d.isoformat()} 晚于今日")
        v = float(days)
        date_ev = _raw_ev("list_date", d.isoformat(), unit="", period=period, as_of=_resp_time(resp), sources=[src])
        ev = Evidence(
            metric=mid,
            value=v,
            unit=md.unit,
            display=fmt_value(v, md.unit),
            period=period,
            as_of=date_to_datetime(self.as_of),
            sources=[src],
            derived=True,
            formula=f"{md.formula} = {self.as_of.isoformat()} − {d.isoformat()}",
            inputs=[date_ev],
        )
        return MetricValue(metric=mid, status="ok", value=v, evidence=ev)


# ───────────────────────────── 入口 ─────────────────────────────


def compute_stock_metrics(
    stock: StockRaw,
    metrics: Iterable[str],
    report: ReportPeriod,
    as_of: date,
    *,
    benchmark: Fetched | None = None,
    trade_date: date | None = None,
) -> dict[str, MetricValue]:
    """单股：为 metrics 中每个登记在 catalog 的指标给出 MetricValue（未知指标忽略）。"""
    calc = _Calc(stock, report, as_of, benchmark, trade_date)
    return {m: calc.metric(m) for m in sorted(set(metrics)) if m in METRICS}


def compute_features(raw: RawBundle, metrics: Iterable[str], report: ReportPeriod, as_of: date) -> dict[str, StockFeatures]:
    """全部股票：dict[thscode, StockFeatures]。纯函数，同输入同输出。"""
    ms = sorted({m for m in metrics if m in METRICS})
    return {
        code: StockFeatures(
            info=s.info,
            metrics=compute_stock_metrics(s, ms, report, as_of, benchmark=raw.benchmark, trade_date=raw.trade_date),
        )
        for code, s in raw.stocks.items()
    }
