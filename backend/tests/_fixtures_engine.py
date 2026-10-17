"""引擎测试用的合成数据构造器。所有数值均为虚构，不对应任何真实行情或财务数据。

回测、服务层测试也可复用，例如：
    from tests._fixtures_engine import make_condition, make_snapshot, make_stock, make_strategy, synthetic_snapshot

约定：
- make_stock 的 metrics 取值：数字/布尔 → ok；None → missing；MetricValue → 原样使用。
- 快照的时间、报告期固定，保证测试确定性。
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app import catalog
from app.schemas import (
    Condition,
    DatasetStatus,
    Evidence,
    FeatureSnapshot,
    MetricValue,
    ReportPeriod,
    ResolvedUniverse,
    SourceRef,
    StockFeatures,
    StockInfo,
    Strategy,
    Universe,
)

CN_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 10, 16, 15, 0, tzinfo=CN_TZ)
REPORT_PERIOD = ReportPeriod(report="2026-2", label="2026年中报", yoy_report="2025-2", quarters=2, rule="测试固定报告期")
SYNTHETIC_NOTE = "测试合成数据（虚构）"

MetricInput = float | int | bool | MetricValue | None


# ───────────────────────────── 指标值 ─────────────────────────────


def _unit(metric: str) -> str:
    m = catalog.METRICS.get(metric)
    return m.unit if m else ""


def _display(value: float | bool | None, unit: str) -> str:
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "是" if value else "否"
    if not math.isfinite(float(value)):
        return "--"
    text = f"{float(value):.2f}"
    return f"{text}{unit}" if unit == "%" else (f"{text} {unit}" if unit else text)


def metric_value(
    metric: str,
    value: float | bool | None = None,
    *,
    status: str = "ok",
    reason: str | None = None,
    stale: bool = False,
    evidence: bool = True,
) -> MetricValue:
    """构造单个指标值；evidence=True 时附带一条合成 Evidence（provider="system"）。"""
    unit = _unit(metric)
    ev = None
    if evidence:
        ev = Evidence(
            metric=metric, value=value, unit=unit, display=_display(value, unit), period="测试期间",
            as_of=FIXED_NOW, sources=[SourceRef(provider="system", field=metric)], note=SYNTHETIC_NOTE,
        )
    return MetricValue(metric=metric, status=status, value=value, evidence=ev, reason=reason, stale=stale)  # type: ignore[arg-type]


def ok(metric: str, value: float | bool, **kw: Any) -> MetricValue:
    return metric_value(metric, value, **kw)


def missing(metric: str, reason: str = "接口返回空值（该期未披露或不适用）") -> MetricValue:
    return metric_value(metric, None, status="missing", reason=reason)


def error(metric: str, reason: str = "数据集请求失败：code=5003 上游服务不可用 request_id=req-test") -> MetricValue:
    return metric_value(metric, None, status="error", reason=reason)


def not_applicable(metric: str, reason: str = "该指标对该股不适用（测试）", value: float | None = None) -> MetricValue:
    return metric_value(metric, value, status="not_applicable", reason=reason)


# ───────────────────────────── 股票与快照 ─────────────────────────────


def make_stock(
    code: str,
    metrics: Mapping[str, MetricInput] | None = None,
    *,
    name: str | None = None,
    industry: str | None = None,
    industry_code: str | None = None,
    list_date: str | None = None,
    evidence: bool = True,
) -> StockFeatures:
    mvs: dict[str, MetricValue] = {}
    for k, v in (metrics or {}).items():
        if isinstance(v, MetricValue):
            mvs[k] = v if v.metric == k else v.model_copy(update={"metric": k})
        elif v is None:
            mvs[k] = missing(k)
        else:
            mvs[k] = metric_value(k, v, evidence=evidence)
    info = StockInfo(
        thscode=code, ticker=code.split(".")[0], name=name or f"测试{code[:6]}",
        industry=industry, industry_code=industry_code, list_date=list_date,
    )
    return StockFeatures(info=info, metrics=mvs)


def reference_from(stocks: Iterable[StockFeatures], metrics: Sequence[str]) -> dict[str, dict[str, float]]:
    """用快照内股票自身的 ok 数值构造参考组（metric -> {thscode: value}）。"""
    ref: dict[str, dict[str, float]] = {m: {} for m in metrics}
    for st in stocks:
        for m in metrics:
            mv = st.metrics.get(m)
            if mv is not None and mv.status == "ok" and isinstance(mv.value, (int, float)) and not isinstance(mv.value, bool):
                ref[m][st.info.thscode] = float(mv.value)
    return ref


def make_snapshot(
    stocks: Iterable[StockFeatures] | Mapping[str, StockFeatures],
    *,
    reference: dict[str, dict[str, float]] | None = None,
    reference_industry: dict[str, str] | None = None,
    industry_names: dict[str, str] | None = None,
    prefiltered: bool = False,
    universe_name: str = "测试股票池",
    universe_kind: str = "custom",
    universe_code: str | None = None,
    snapshot_id: str = "snap-test",
    datasets: list[DatasetStatus] | None = None,
    trade_date: str = "2026-10-16",
) -> FeatureSnapshot:
    items = list(stocks.values()) if isinstance(stocks, Mapping) else list(stocks)
    by_code = {s.info.thscode: s for s in items}
    if reference_industry is None:
        reference_industry = {c: s.info.industry_code for c, s in by_code.items() if s.info.industry_code}
    metrics_available = sorted({m for s in items for m in s.metrics})
    universe = ResolvedUniverse(
        kind=universe_kind, code=universe_code, name=universe_name, size=len(by_code),  # type: ignore[arg-type]
        members=sorted(by_code), note="测试合成股票池",
    )
    return FeatureSnapshot(
        snapshot_id=snapshot_id, universe=universe, stocks=by_code, reference=reference or {},
        reference_industry=reference_industry, industry_names=industry_names or {},
        datasets=datasets or [], report_period=REPORT_PERIOD, trade_date=trade_date,
        metrics_available=metrics_available, prefiltered=prefiltered, created_at=FIXED_NOW,
    )


# ───────────────────────────── 条件与策略 ─────────────────────────────


def make_condition(
    metric: str,
    op: str,
    value: float | None = None,
    value2: float | None = None,
    *,
    scope: str | None = None,
    hard: bool = True,
    weight: float = 1.0,
    cid: str = "",
    enabled: bool = True,
    origin: str = "user",
    source_phrase: str | None = None,
) -> Condition:
    return Condition(
        id=cid, metric=metric, op=op, value=value, value2=value2, rank_scope=scope, hard=hard,  # type: ignore[arg-type]
        weight=weight, enabled=enabled, origin=origin, source_phrase=source_phrase,  # type: ignore[arg-type]
    )


def make_strategy(
    *conditions: Condition,
    policy: str = "exclude",
    universe: Universe | None = None,
    sort_metric: str | None = None,
    sort_desc: bool = True,
    name: str | None = None,
    query: str | None = None,
    report_period: str | None = None,
) -> Strategy:
    return Strategy(
        name=name, query=query, universe=universe or Universe(kind="custom", code=None, name="测试股票池"),
        conditions=list(conditions), missing_policy=policy, report_period=report_period,  # type: ignore[arg-type]
        sort_metric=sort_metric, sort_desc=sort_desc,
    )


# ───────────────────────────── 大规模合成快照 ─────────────────────────────

# 各指标的虚构分布（只为覆盖取值范围，与真实市场无关）
_GENERATORS: dict[str, Any] = {
    "pe_ttm": lambda r: r.gauss(25, 12) if r.random() < 0.85 else -abs(r.gauss(30, 20)),
    "pb_mrq": lambda r: abs(r.gauss(2.5, 1.5)) if r.random() < 0.97 else -abs(r.gauss(1, 0.5)),
    "ps_ttm": lambda r: abs(r.gauss(3, 2)),
    "pcf_ttm": lambda r: r.gauss(15, 20),
    "revenue_yoy": lambda r: r.gauss(8, 25),
    "net_profit_yoy": lambda r: r.gauss(5, 40),
    "op_profit_yoy": lambda r: r.gauss(5, 40),
    "roe": lambda r: r.gauss(5, 4),
    "roe_annualized": lambda r: r.gauss(10, 8),
    "roe_change": lambda r: r.gauss(0, 2),
    "roe_deducted": lambda r: r.gauss(4.5, 4),
    "gross_margin": lambda r: r.uniform(5, 70),
    "gross_margin_change": lambda r: r.gauss(0, 3),
    "net_margin": lambda r: r.gauss(10, 8),
    "ocf_to_profit": lambda r: r.gauss(90, 50),
    "ocf_to_revenue": lambda r: r.gauss(10, 10),
    "debt_ratio": lambda r: r.uniform(10, 85),
    "current_ratio": lambda r: abs(r.gauss(1.8, 0.8)),
    "volatility_250d": lambda r: r.uniform(15, 60),
    "max_drawdown_250d": lambda r: r.uniform(8, 60),
    "beta_250d": lambda r: r.gauss(1.0, 0.3),
    "return_20d": lambda r: r.gauss(1, 8),
    "return_60d": lambda r: r.gauss(2, 15),
    "return_250d": lambda r: r.gauss(5, 30),
    "avg_amount_20d": lambda r: r.lognormvariate(0.5, 1.0),
    "pct_change_today": lambda r: r.gauss(0, 2),
    "market_cap_est": lambda r: r.lognormvariate(5, 1),
    "dividend_yield_ttm": lambda r: r.uniform(0, 6),
    "revenue_amount": lambda r: r.lognormvariate(3, 1.5),
    "parent_np_amount": lambda r: r.gauss(5, 10),
    "is_st": lambda r: r.random() < 0.03,
    "listing_days": lambda r: float(r.randint(30, 9000)),
}
CHEAP_REFERENCE_METRICS = ("pe_ttm", "pb_mrq", "ps_ttm", "pcf_ttm", "pct_change_today")


def synthetic_codes(n: int) -> list[str]:
    """虚构代码序列：前一半 6xxxxx.SH，后一半 00xxxx.SZ。"""
    half = (n + 1) // 2
    return [f"{600000 + i:06d}.SH" for i in range(half)] + [f"{1 + i:06d}.SZ" for i in range(n - half)]


def synthetic_snapshot(
    n_stocks: int = 1000,
    *,
    seed: int = 7,
    metrics: Sequence[str] | None = None,
    missing_rate: float = 0.03,
    n_industries: int = 20,
    with_reference: bool = True,
    evidence: bool = False,
) -> FeatureSnapshot:
    """按固定种子生成的大规模合成快照（虚构数值），用于性能、一致性与回归测试。"""
    rng = random.Random(seed)
    metric_ids = list(metrics) if metrics is not None else list(_GENERATORS)
    stocks: list[StockFeatures] = []
    for i, code in enumerate(synthetic_codes(n_stocks)):
        ind = f"T{rng.randrange(n_industries):02d}"
        vals: dict[str, MetricInput] = {}
        for mid in metric_ids:
            if rng.random() < missing_rate:
                vals[mid] = missing(mid)
                continue
            raw = _GENERATORS[mid](rng)
            vals[mid] = raw if isinstance(raw, bool) else round(float(raw), 2)
        stocks.append(make_stock(code, vals, name=f"合成股{i:04d}", industry=f"测试行业{ind[1:]}",
                                 industry_code=ind, evidence=evidence))
    reference = reference_from(stocks, [m for m in CHEAP_REFERENCE_METRICS if m in metric_ids]) if with_reference else {}
    names = {f"T{k:02d}": f"测试行业{k:02d}" for k in range(n_industries)}
    return make_snapshot(stocks, reference=reference, industry_names=names, universe_name="合成股票池")
