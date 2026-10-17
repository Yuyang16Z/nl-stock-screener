"""回测引擎测试：全部为合成数据（股票代码仅作示例，所有数值均为虚构），不访问网络。"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from app.catalog import METRICS
from app.engine.backtest import (
    CN_TZ,
    SUPPORTED,
    BacktestData,
    StockSeries,
    annualized_volatility,
    beta_coefficient,
    income_metrics_as_of,
    max_drawdown_pct,
    perf_stats,
    period_return_pct,
    rebalance_dates,
    run_backtest,
)
from app.schemas import BacktestRequest, BacktestResult, Condition, DatasetStatus, StockInfo, Strategy

# ───────────────────────────── 合成数据工具 ─────────────────────────────


def weekdays(start: str, end: str) -> list[str]:
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


CAL = weekdays("2022-01-03", "2024-06-28")  # 合成交易日历：工作日
IDX = {d: i for i, d in enumerate(CAL)}
N = len(CAL)


def ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=CN_TZ).timestamp() * 1000)


def path(returns: Sequence[float], p0: float = 10.0) -> list[float]:
    """returns[i] 为第 i 日收益（returns[0] 忽略）。"""
    out = [p0]
    for r in returns[1:]:
        out.append(out[-1] * (1.0 + r))
    return out


def alternating(up: float, down: float, n: int = N) -> list[float]:
    return [0.0] + [up if i % 2 else down for i in range(1, n)]


def step_series(jump_after: int, r: float, p0: float = 10.0, turnover: float | None = 1e8) -> StockSeries:
    """日历下标 ≤ jump_after 时价格为 p0，之后为 p0 × (1 + r)。"""
    closes = [p0 if i <= jump_after else p0 * (1 + r) for i in range(N)]
    return StockSeries(list(CAL), closes, turnover=[turnover] * N if turnover is not None else None)


def flat(p: float = 10.0, turnover: float = 1e8, raw: float | None = None) -> StockSeries:
    return StockSeries(list(CAL), [p] * N, close_raw=[raw] * N if raw is not None else None, turnover=[turnover] * N)


def info_for(codes, **overrides: StockInfo) -> dict[str, StockInfo]:
    out = {c: StockInfo(thscode=c, ticker=c[:6], name=f"示例{c[:6]}", list_date="2010-01-04") for c in codes}
    out.update(overrides)
    return out


def make_data(
    prices: dict[str, StockSeries],
    *,
    bench: dict[str, float] | None = None,
    income: dict[str, list[dict]] | None = None,
    info: dict[str, StockInfo] | None = None,
    members: list[str] | None = None,
    datasets: list[DatasetStatus] | None = None,
) -> BacktestData:
    if bench is None:
        bench = {d: 1000.0 * 1.0002**i for i, d in enumerate(CAL)}
    return BacktestData(
        trade_dates=list(CAL),
        benchmark_close=bench,
        prices=prices,
        income=income or {},
        info=info if info is not None else info_for(prices),
        members=list(members if members is not None else prices),
        datasets=datasets or [],
    )


def make_req(conditions=(), start: str = "2023-03-01", end: str | None = "2023-08-31", **kw) -> BacktestRequest:
    skw = {k: kw.pop(k) for k in ("sort_metric", "sort_desc", "missing_policy", "report_period") if k in kw}
    return BacktestRequest(strategy=Strategy(conditions=list(conditions), **skw), start=start, end=end, **kw)


def run(req: BacktestRequest, data: BacktestData, **kw) -> BacktestResult:
    return run_backtest(req, data, backtest_id="bt-test", **kw)


def cond(cid: str, metric: str, op: str, value=None, value2=None, **kw) -> Condition:
    return Condition(id=cid, metric=metric, op=op, value=value, value2=value2, **kw)


_PE = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
_Q = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 4}


def inc(year: int, fp: str, report_day: str | None, **values) -> dict:
    """利润表季报 item（字段与扶摇一致，数值虚构）。"""
    item = {
        "thscode": "600000.SH",
        "ticker": "600000",
        "period": "quarterly",
        "fiscal_year": year,
        "fiscal_period": fp,
        "report_date_ms": ms(report_day) if report_day else None,
        "period_end_ms": ms(f"{year}-{_PE[_Q[fp]]}"),
        "currency": "CNY",
        "operating_income": None,
        "operating_costs": None,
        "operating_profit": None,
        "net_profit": None,
        "parent_holder_net_profit": None,
        "basic_eps": None,
    }
    item.update(values)
    return item


def curve_map(res: BacktestResult) -> dict[str, float]:
    return {p.date: p.strategy for p in res.curve}


def holdings_by_date(res: BacktestResult) -> dict[str, list[str]]:
    return {r.date: r.holdings for r in res.rebalances}


# ───────────────────────────── 目录一致性 ─────────────────────────────


def test_supported_set_matches_catalog_backtestable():
    assert SUPPORTED == {m.id for m in METRICS.values() if m.backtestable}


# ───────────────────────────── 净值与统计 ─────────────────────────────


def test_single_stock_nav_tracks_price_and_stats_match_formulas():
    closes = path(alternating(0.01, -0.006))
    bench_closes = path(alternating(0.005, -0.003), p0=1000.0)
    data = make_data(
        {"600001.SH": StockSeries(list(CAL), closes)},
        bench={d: bench_closes[i] for i, d in enumerate(CAL)},
    )
    res = run(make_req([], "2023-03-01", "2023-08-31", top_n=None, cost_bps=0.0), data)
    assert res.status == "done" and res.error is None

    t0, t1 = IDX["2023-03-01"], IDX["2023-08-31"]
    nav = np.array([closes[t] / closes[t0] for t in range(t0, t1 + 1)])
    bnav = np.array([bench_closes[t] / bench_closes[t0] for t in range(t0, t1 + 1)])
    assert [p.date for p in res.curve] == CAL[t0 : t1 + 1]
    assert res.curve[0].strategy == 1.0 and res.curve[0].benchmark == 1.0
    assert [p.strategy for p in res.curve] == pytest.approx(nav.tolist(), abs=2e-6)
    assert [p.benchmark for p in res.curve] == pytest.approx(bnav.tolist(), abs=2e-6)

    def expected(v: np.ndarray) -> dict[str, float]:
        r = v[1:] / v[:-1] - 1
        sd = np.std(r, ddof=1)
        return {
            "total_return": (v[-1] - 1) * 100,
            "annual_return": (v[-1] ** (252 / r.size) - 1) * 100,
            "annual_vol": sd * math.sqrt(252) * 100,
            "max_drawdown": float(np.max(1 - v / np.maximum.accumulate(v))) * 100,
            "sharpe": r.mean() / sd * math.sqrt(252),
        }

    for key, val in expected(nav).items():
        assert getattr(res.stats, key) == pytest.approx(val, abs=1e-3), key
    for key, val in expected(bnav).items():
        assert getattr(res.benchmark_stats, key) == pytest.approx(val, abs=1e-3), key
    assert res.stats.excess_total_return == pytest.approx((nav[-1] - bnav[-1]) * 100, abs=1e-3)
    assert res.stats.avg_holdings == 1.0
    assert res.stats.avg_turnover == pytest.approx(0.0, abs=1e-9)  # 单只股票，建仓后无换手
    assert res.benchmark_stats.excess_total_return is None


def test_two_stocks_equal_weight_drift_rebalance_and_cost():
    a = [10.0 * 1.01**i for i in range(N)]  # 每日 +1%
    b = [20.0] * N  # 不变
    data = make_data({"600001.SH": StockSeries(list(CAL), a), "600002.SH": StockSeries(list(CAL), b)})
    start, end = "2023-01-02", "2023-06-30"
    free = run(make_req([], start, end, top_n=None, cost_bps=0.0), data)
    paid = run(make_req([], start, end, top_n=None, cost_bps=25.0), data)

    rdates = [r.date for r in free.rebalances]
    assert rdates == ["2023-01-02", "2023-02-01", "2023-03-01", "2023-04-03", "2023-05-01", "2023-06-01"]
    assert all(r.holdings == ["600001.SH", "600002.SH"] for r in free.rebalances)
    bounds = [IDX[d] for d in rdates] + [IDX[end]]
    lengths = [bounds[k + 1] - bounds[k] for k in range(len(rdates))]

    # 无成本：每个持有期净值乘以 (½·1.01^m + ½)
    nav_free = math.prod(0.5 * 1.01**m + 0.5 for m in lengths)
    assert free.curve[-1].strategy == pytest.approx(nav_free, abs=2e-6)

    # 有成本：建仓买入 100%（一边），之后每次买卖两边 = 2·|wA − ½|
    c = 25.0 / 1e4
    drift_w = [1.01**m / (1.01**m + 1) for m in lengths[:-1]]
    factors = [1 - c] + [1 - 2 * abs(w - 0.5) * c for w in drift_w]
    assert paid.curve[-1].strategy == pytest.approx(nav_free * math.prod(factors), abs=2e-6)
    assert paid.stats.total_return < free.stats.total_return

    # 调仓日的点为调仓前市值：第 k 次调仓成本在下一交易日起体现
    fm, pm = curve_map(free), curve_map(paid)
    for k, d in enumerate(rdates):
        nxt = CAL[IDX[d] + 1]
        assert pm[d] / fm[d] == pytest.approx(math.prod(factors[:k]), abs=1e-5)
        assert pm[nxt] / fm[nxt] == pytest.approx(math.prod(factors[: k + 1]), abs=1e-5)

    # 平均单边换手（不含首次建仓）= mean(|wA − ½|)
    assert free.stats.avg_turnover == pytest.approx(float(np.mean([abs(w - 0.5) for w in drift_w])) * 100, abs=1e-3)
    assert "单边换手 100.0%" in paid.rebalances[0].note
    assert "交易成本 0.250%" in paid.rebalances[0].note


def test_curve_starts_at_one_even_with_cost():
    data = make_data({"600001.SH": flat()})
    res = run(make_req([], "2023-03-01", "2023-03-31", cost_bps=30.0), data)
    assert res.curve[0].strategy == 1.0 and res.curve[0].benchmark == 1.0
    assert len(res.curve) == len(weekdays("2023-03-01", "2023-03-31"))
    assert res.curve[1].strategy == pytest.approx(1 - 30.0 / 1e4, abs=1e-9)  # 建仓成本自下一日体现


# ───────────────────────────── 时点一致 ─────────────────────────────


def test_price_conditions_use_previous_close_only():
    d0 = IDX["2023-03-01"]
    data = make_data(
        {
            "600001.SH": step_series(jump_after=d0 - 1, r=0.5),  # 调仓日当天才上涨 50%
            "600002.SH": step_series(jump_after=d0 - 2, r=0.5),  # 调仓日前一交易日上涨 50%
        }
    )
    res = run(make_req([cond("c1", "return_20d", "gt", 10)], "2023-03-01", "2023-03-10"), data)
    assert res.rebalances[0].holdings == ["600002.SH"]


def test_income_report_used_only_after_disclosure():
    def items(q3_report: str) -> list[dict]:
        return [
            inc(2022, "Q2", "2022-08-20", operating_income=100.0),
            inc(2022, "Q3", "2022-10-25", operating_income=150.0),
            inc(2023, "Q2", "2023-08-25", operating_income=90.0),  # 同比 −10%
            inc(2023, "Q3", q3_report, operating_income=225.0),  # 同比 +50%
        ]

    codes = ["600001.SH", "600002.SH", "600003.SH"]
    data = make_data(
        {c: flat() for c in codes},
        income={"600001.SH": items("2023-10-30"), "600002.SH": items("2023-11-01")},  # 600003 无利润表
    )
    res = run(make_req([cond("c1", "revenue_yoy", "gt", 0)], "2023-09-01", "2023-12-29"), data)
    assert holdings_by_date(res) == {
        "2023-09-01": [],
        "2023-10-02": [],
        # 600002 的披露日 11-01 晚于三季报法定截止日 10-31：真实数据会把历史期披露日覆盖成后续报告日期，
        # 因此晚于截止日的一律按截止日视为可得（10-31 早于调仓日 11-01，可用）
        "2023-11-01": ["600001.SH", "600002.SH"],
        "2023-12-01": ["600001.SH", "600002.SH"],
    }
    first = res.rebalances[0].note
    assert "空仓" in first and "2 只未通过硬条件" in first and "1 只因数据缺失或不适用未入选" in first
    assert any("披露" in a and "早于调仓日" in a for a in res.assumptions)


def test_report_date_not_after_period_end_falls_back_to_legal_deadline():
    items = [
        inc(2022, "Q2", "2022-08-20", operating_income=100.0),
        inc(2022, "Q3", "2022-10-28", operating_income=100.0),
        inc(2023, "Q2", "2023-08-20", operating_income=80.0),
        inc(2023, "Q3", "2023-09-30", operating_income=130.0),  # 披露日 = 报告期末：不可信，按 10/31 截止日
    ]
    early = income_metrics_as_of(items, "2023-10-09", metrics=["revenue_yoy"])["revenue_yoy"]
    late = income_metrics_as_of(items, "2023-11-01", metrics=["revenue_yoy"])["revenue_yoy"]
    assert early.status == "ok" and early.value == pytest.approx(-20.0)
    assert late.status == "ok" and late.value == pytest.approx(30.0)
    # 缺披露日同样按法定截止日
    no_date = [inc(2022, "Q1", "2022-04-20", operating_income=50.0), inc(2023, "Q1", None, operating_income=60.0)]
    assert income_metrics_as_of(no_date, "2023-04-28", metrics=["revenue_yoy"])["revenue_yoy"].status == "missing"
    assert income_metrics_as_of(no_date, "2023-05-02", metrics=["revenue_yoy"])["revenue_yoy"].value == pytest.approx(20.0)


def test_income_growth_and_margin_formulas():
    items = [
        inc(2022, "Q2", "2022-08-20", operating_income=150.0, operating_costs=100.0, net_profit=20.0, operating_profit=-20.0),
        inc(2023, "Q2", "2023-08-20", operating_income=200.0, operating_costs=120.0, net_profit=30.0, operating_profit=-10.0),
    ]
    mv = income_metrics_as_of(items, "2023-09-01")
    assert mv["revenue_yoy"].value == pytest.approx(100 * 50 / 150)
    assert mv["net_profit_yoy"].value == pytest.approx(50.0)
    assert mv["op_profit_yoy"].value == pytest.approx(50.0)  # (−10 − (−20)) ÷ |−20|
    assert mv["gross_margin"].value == pytest.approx(40.0)
    assert mv["gross_margin_change"].value == pytest.approx(40.0 - 100 * 50 / 150)

    zero_base = [inc(2022, "Q2", "2022-08-20", net_profit=0.0), inc(2023, "Q2", "2023-08-20", net_profit=5.0)]
    z = income_metrics_as_of(zero_base, "2023-09-01", metrics=["net_profit_yoy"])["net_profit_yoy"]
    assert z.status == "missing" and "为 0" in (z.reason or "")

    bank_like = [inc(2023, "Q2", "2023-08-20", operating_income=200.0, operating_costs=None)]
    g = income_metrics_as_of(bank_like, "2023-09-01", metrics=["gross_margin", "revenue_yoy"])
    assert g["gross_margin"].status == "missing" and "营业成本" in (g["gross_margin"].reason or "")
    assert g["revenue_yoy"].status == "missing" and "上年同期" in (g["revenue_yoy"].reason or "")

    stale = [inc(2021, "Q3", "2021-10-20", operating_income=1.0)]
    s = income_metrics_as_of(stale, "2023-03-01", metrics=["revenue_yoy"])["revenue_yoy"]
    assert s.status == "missing" and "过旧" in (s.reason or "")


# ───────────────────────────── PE 近似重建 ─────────────────────────────

PE_ITEMS = [
    inc(2022, "Q1", "2022-04-28", basic_eps=0.5),
    inc(2022, "FY", "2023-03-20", basic_eps=2.0),
    inc(2023, "Q1", "2023-04-25", basic_eps=0.6),
    inc(2023, "Q4", "2024-03-22", basic_eps=2.4),
]


def test_pe_ttm_reconstruction_unit():
    def pe(day: str, raw: float | None):
        return income_metrics_as_of(PE_ITEMS, day, raw_close=raw, metrics=["pe_ttm"])["pe_ttm"]

    assert pe("2023-04-03", 21.0).value == pytest.approx(10.5)  # 年报：EPS_TTM = 2.0
    assert pe("2023-05-04", 21.0).value == pytest.approx(10.0)  # 0.6 + 2.0 − 0.5 = 2.1
    assert pe("2024-04-01", 24.0).value == pytest.approx(10.0)  # 2023 年报 2.4
    assert pe("2023-05-04", None).status == "missing"
    no_same = [inc(2022, "FY", "2023-03-20", basic_eps=2.0), inc(2023, "Q1", "2023-04-25", basic_eps=0.6)]
    r = income_metrics_as_of(no_same, "2023-05-04", raw_close=21.0, metrics=["pe_ttm"])["pe_ttm"]
    assert r.status == "missing" and "上年同期" in (r.reason or "")


def test_pe_ttm_condition_end_to_end_and_missing_without_raw_close():
    data = make_data({"600001.SH": flat(p=55.0, raw=21.0)}, income={"600001.SH": PE_ITEMS})
    req = make_req([cond("c1", "pe_ttm", "between", 9.9, 10.1)], "2023-04-03", "2023-06-30")
    res = run(req, data)
    assert holdings_by_date(res) == {"2023-04-03": [], "2023-05-01": ["600001.SH"], "2023-06-01": ["600001.SH"]}
    assert any("市盈率TTM 为近似重建" in a for a in res.assumptions)

    # 无 close_raw：条件不丢弃，逐股按缺失处理 → 不入选，并在 assumptions 说明
    no_raw = make_data({"600001.SH": flat(p=55.0)}, income={"600001.SH": PE_ITEMS})
    res2 = run(req, no_raw)
    assert res2.status == "done" and res2.ignored_conditions == []
    assert all(r.holdings == [] for r in res2.rebalances)
    assert "1 只因数据缺失或不适用未入选" in res2.rebalances[1].note
    assert any("不复权收盘价全部缺失" in a and "不会视为通过" in a for a in res2.assumptions)


def test_dataset_wide_absence_never_passes_hard_condition():
    # 利润表整体缺失（如演示故障注入）：成长类硬条件对所有股票缺失 → 空仓，而不是忽略条件后放行
    data = make_data({"600001.SH": flat(), "600002.SH": flat()}, income={})
    res = run(make_req([cond("c1", "revenue_yoy", "gt", 0)], "2023-03-01", "2023-05-31"), data)
    assert res.ignored_conditions == []
    assert all(r.holdings == [] for r in res.rebalances)
    assert any("没有任何利润表季报数据" in a for a in res.assumptions)
    # 同样的数据缺失落在软条件上：只是不得分，不影响入选
    soft = run(make_req([cond("s1", "revenue_yoy", "gt", 0, hard=False)], "2023-03-01", "2023-05-31"), data)
    assert soft.rebalances[0].holdings == ["600001.SH", "600002.SH"]
    assert any("软条件下所有股票不得分" in a for a in soft.assumptions)


# ───────────────────────────── 空仓 / 截断 / 排序 ─────────────────────────────


def test_empty_portfolio_when_nothing_passes():
    data = make_data({"600001.SH": flat(), "600002.SH": flat()})
    res = run(make_req([cond("c1", "listing_days", "gte", 100000)], "2023-03-01", "2023-06-30"), data)
    assert res.status == "done"
    assert all(r.holdings == [] and r.candidates == 0 for r in res.rebalances)
    assert all("空仓" in (r.note or "") for r in res.rebalances)
    assert all(p.strategy == 1.0 for p in res.curve)
    assert res.stats.total_return == 0.0 and res.stats.max_drawdown == 0.0
    assert res.stats.sharpe is None  # 零波动不报夏普
    assert res.stats.avg_holdings == 0.0


def _ranking_universe() -> dict[str, StockSeries]:
    c = IDX["2023-03-01"] - 1  # 调仓日前一交易日
    lo = c - 20  # return_20d 窗口起点
    short = [None] * N
    for i in range(c - 9, N):
        short[i] = 10.0  # 只有近 10 日价格：窗口样本不足
    return {
        "600101.SH": step_series(lo, 0.05, turnover=1e8),
        "600102.SH": step_series(lo, 0.03, turnover=3e8),
        "600103.SH": step_series(lo, -0.02, turnover=9e8),
        "600104.SH": step_series(lo, 0.01, turnover=3e8),
        "600105.SH": StockSeries(list(CAL), short, turnover=[5e8 if x else None for x in short]),
    }


def test_top_n_truncation_soft_score_then_sort_metric_then_code():
    data = make_data(_ranking_universe())
    soft = cond("s1", "return_20d", "gt", 0, hard=False)
    res = run(make_req([soft], "2023-03-01", "2023-03-10", top_n=2, sort_metric="avg_amount_20d"), data)
    rec = res.rebalances[0]
    # 软分 1：600101(1亿)、600102(3亿)、600104(3亿)；软分 0：600103(9亿)、600105(缺失)
    assert rec.holdings == ["600102.SH", "600104.SH"]
    assert rec.candidates == 5
    assert "超过 top_n" in rec.note and "取前 2 只" in rec.note
    assert rec.names == ["示例600102", "示例600104"]

    asc = run(make_req([soft], "2023-03-01", "2023-03-10", top_n=2, sort_metric="avg_amount_20d", sort_desc=False), data)
    assert asc.rebalances[0].holdings == ["600101.SH", "600102.SH"]

    no_limit = run(make_req([soft], "2023-03-01", "2023-03-10", top_n=None, sort_metric="avg_amount_20d"), data)
    assert no_limit.rebalances[0].holdings == ["600102.SH", "600104.SH", "600101.SH", "600103.SH", "600105.SH"]


def test_unsupported_sort_metric_is_reported():
    data = make_data(_ranking_universe())
    res = run(make_req([], "2023-03-01", "2023-03-10", top_n=2, sort_metric="roe"), data)
    assert any("排序指标" in a and "不支持回测" in a for a in res.assumptions)
    assert res.rebalances[0].holdings == ["600101.SH", "600102.SH"]  # 退化为按代码


# ───────────────────────────── 分位条件 ─────────────────────────────


def test_rank_condition_within_universe_and_small_group_not_applicable():
    lo = IDX["2023-03-01"] - 21
    rets = {"600201.SH": -0.03, "600202.SH": -0.01, "600203.SH": 0.01, "600204.SH": 0.02, "600205.SH": 0.04, "600206.SH": 0.06}
    prices = {c: step_series(lo, r) for c, r in rets.items()}
    c1 = cond("c1", "return_20d", "pct_rank_gte", 50, rank_scope="universe")
    res = run(make_req([c1], "2023-03-01", "2023-03-10", top_n=None), make_data(prices))
    assert res.rebalances[0].holdings == ["600204.SH", "600205.SH", "600206.SH"]  # 分位 60/80/100

    # 只有 4 只有效（< MIN_RANK_GROUP=5）→ 不适用 → 不入选
    small = dict(prices)
    for c in ("600201.SH", "600202.SH"):
        small[c] = StockSeries(list(CAL), [None] * N)
    res2 = run(make_req([c1], "2023-03-01", "2023-03-10", top_n=None), make_data(small))
    assert res2.rebalances[0].holdings == []
    assert "6 只因数据缺失或不适用未入选" in res2.rebalances[0].note


def test_industry_scope_downgraded_to_universe_and_reported():
    # pe_ttm 允许行业分位：回测降级为股票池内分位。PE = 不复权价 ÷ 2.1 → 价格越低分位越低
    codes = [f"60030{i}.SH" for i in range(1, 7)]
    prices = {c: flat(p=50.0, raw=10.0 + i) for i, c in enumerate(codes)}
    c1 = cond("c1", "pe_ttm", "pct_rank_lte", 50, rank_scope="industry")
    data = make_data(prices, income={c: PE_ITEMS for c in codes})
    res = run(make_req([c1], "2023-05-04", "2023-05-31", top_n=None), data)
    assert res.ignored_conditions == []
    assert res.rebalances[0].holdings == codes[:3]  # 分位 0/20/40
    assert any("所属行业内分位在回测中降级为调仓日股票池内分位" in a for a in res.assumptions)


# ───────────────────────────── 忽略条件 ─────────────────────────────


def test_ignored_conditions_are_listed_with_chinese_reasons():
    prices = {f"60040{i}.SH": flat() for i in range(1, 7)}
    conds = [
        cond("c1", "roe_annualized", "gte", 15),
        cond("c2", "foo_metric", "gt", 0),
        cond("c3", "return_20d", "pct_rank_gte", 0, rank_scope="market"),
        cond("c4", "return_20d", "gt", None),
        cond("c5", "is_st", "gt", 0),
        cond("c6", "return_60d", "gt", 0, enabled=False),
        cond("c7", "pb_mrq", "lt", 1),
        cond("c8", "return_20d", "between", 0, None),
        cond("c9", "return_20d", "pct_rank_gte", 150, rank_scope="universe"),
        cond("c10", "return_20d", "pct_rank_gte", 50),  # 未指定参考范围
        cond("c11", "return_20d", "pct_rank_gte", 0, rank_scope="universe"),  # 合法：全部通过
    ]
    res = run(make_req(conds, "2023-03-01", "2023-03-10"), make_data(prices))
    assert res.status == "done"
    ignored = {i.condition_id: i for i in res.ignored_conditions}
    assert set(ignored) == {"c1", "c2", "c3", "c4", "c5", "c7", "c8", "c9", "c10"}
    assert "单期数据" in ignored["c1"].reason
    assert "不在指标目录" in ignored["c2"].reason
    assert "不支持全A股内分位" in ignored["c3"].reason  # 与筛选侧结构校验一致
    assert "阈值" in ignored["c4"].reason
    assert "不适用" in ignored["c5"].reason
    assert "估值快照" in ignored["c7"].reason
    assert "上界" in ignored["c8"].reason
    assert "0~100" in ignored["c9"].reason
    assert "参考范围" in ignored["c10"].reason
    for item in res.ignored_conditions:
        assert item.label and re.search(r"[一-鿿]", item.reason)
    assert any("已停用的 1 条条件" in a for a in res.assumptions)
    assert len(res.rebalances[0].holdings) == 6  # 只剩 c11（分位 ≥ 0），全部通过


# ───────────────────────────── 调仓日 ─────────────────────────────


def test_quarterly_and_monthly_rebalance_dates():
    assert [date.fromisoformat(d).weekday() for d in ("2023-04-03", "2023-07-03", "2023-10-02")] == [0, 0, 0]
    assert rebalance_dates(CAL, "2023-02-15", "2023-12-29", "quarterly") == ["2023-02-15", "2023-04-03", "2023-07-03", "2023-10-02"]
    assert rebalance_dates(CAL, "2023-02-15", "2023-05-31", "monthly") == ["2023-02-15", "2023-03-01", "2023-04-03", "2023-05-01"]
    # 区间最后一个交易日恰为月初：不再调仓
    assert rebalance_dates(CAL, "2023-02-15", "2023-05-01", "monthly") == ["2023-02-15", "2023-03-01", "2023-04-03"]
    # 起点为节假日（周末）：取其后首个交易日建仓
    assert rebalance_dates(CAL, "2023-04-01", "2023-05-31", "monthly") == ["2023-04-03", "2023-05-01"]

    data = make_data({"600001.SH": flat()})
    res = run(make_req([], "2023-02-15", "2023-12-29", rebalance="quarterly"), data)
    assert [r.date for r in res.rebalances] == ["2023-02-15", "2023-04-03", "2023-07-03", "2023-10-02"]
    assert any("每季首个交易日" in a for a in res.assumptions)


# ───────────────────────────── 其他条件与口径 ─────────────────────────────


def test_beta_condition_uses_benchmark_returns():
    rm = [0.0] + [((i * 37) % 11 - 5) / 500 for i in range(1, N)]  # 确定性的伪随机基准收益
    bench = path(rm, p0=1000.0)
    high = path([0.0] + [2 * x for x in rm[1:]])
    low = path([0.0] + [0.5 * x for x in rm[1:]])
    data = make_data(
        {"600501.SH": StockSeries(list(CAL), high), "600502.SH": StockSeries(list(CAL), low)},
        bench={d: bench[i] for i, d in enumerate(CAL)},
    )
    res = run(make_req([cond("c1", "beta_250d", "between", 1.99, 2.01)], "2023-03-01", "2023-03-10"), data)
    assert res.rebalances[0].holdings == ["600501.SH"]
    res2 = run(make_req([cond("c1", "beta_250d", "lte", 1.0)], "2023-03-01", "2023-03-10"), data)
    assert res2.rebalances[0].holdings == ["600502.SH"]
    res3 = run(make_req([cond("c1", "beta_250d", "lte", 1.0)], "2023-03-01", "2023-03-10", benchmark="000905.SH"), data)
    assert any("筛选口径为相对沪深300" in a for a in res3.assumptions)


def test_insufficient_window_samples_are_missing_not_pass():
    c = IDX["2023-03-01"] - 1
    partial = [None] * N
    for i in range(c - 149, N):
        partial[i] = 10.0 + 0.01 * i  # 窗口内只有 150 个有效收盘（< 200）
    data = make_data({"600601.SH": StockSeries(list(CAL), partial), "600602.SH": flat()})
    res = run(make_req([cond("c1", "volatility_250d", "lt", 1000)], "2023-03-01", "2023-03-10"), data)
    assert res.rebalances[0].holdings == ["600602.SH"]
    assert "1 只因数据缺失或不适用未入选" in res.rebalances[0].note


def test_window_uses_own_trading_bars_and_skips_zero_turnover_records():
    c = IDX["2023-03-01"] - 1
    turnover = [2e8] * N
    for i in range(c - 9, c + 1):
        turnover[i] = 0.0  # 调仓日前 10 天为零成交（停牌记录，收盘价沿用）
    suspended = StockSeries(list(CAL), [10.0] * N, turnover=turnover)
    data = make_data({"601201.SH": suspended, "601202.SH": flat(turnover=1e8)})
    res = run(make_req([cond("c1", "avg_amount_20d", "gte", 1.9)], "2023-03-01", "2023-03-10"), data)
    # 剔除零成交记录后，最近 20 根有效日K的日均成交额 = 2 亿（与筛选口径一致），而非把停牌日按 0 计入
    assert res.rebalances[0].holdings == ["601201.SH"]

    # 长期停牌后窗口向前取满 N+1 根有效日K（与筛选一致），不因日历空档判为缺失
    gap = [10.0 * (1.002 if i % 2 else 0.999) ** i for i in range(N)]
    for i in range(c - 80, c - 20):
        gap[i] = None
    data2 = make_data({"601203.SH": StockSeries(list(CAL), gap)})
    res2 = run(make_req([cond("c1", "volatility_250d", "lt", 1000)], "2023-03-01", "2023-03-10"), data2)
    assert res2.rebalances[0].holdings == ["601203.SH"]


def test_formulas_match_screening_features_module():
    features = pytest.importorskip("app.data.features")
    names = ("calc_volatility", "calc_max_drawdown", "calc_return", "calc_beta")
    if not all(hasattr(features, n) for n in names):
        pytest.skip("features 模块未提供可比对的纯函数")
    rng = np.random.default_rng(42)
    for _ in range(20):
        p = 10 * np.cumprod(1 + rng.normal(0, 0.02, 120))
        rm = rng.normal(0, 0.01, 119)
        ri = 1.3 * rm + rng.normal(0, 0.005, 119)
        assert annualized_volatility(p) == pytest.approx(features.calc_volatility(p.tolist()), rel=1e-12)
        assert max_drawdown_pct(p) == pytest.approx(features.calc_max_drawdown(p.tolist())[0], rel=1e-12)
        assert period_return_pct(p) == pytest.approx(features.calc_return(p.tolist()), rel=1e-12)
        assert beta_coefficient(ri, rm) == pytest.approx(features.calc_beta(ri.tolist(), rm.tolist()), rel=1e-9)


def test_missing_price_days_carry_forward_previous_close():
    closes: list[float | None] = [10.0 * 1.01**i for i in range(N)]
    gap = [IDX[d] for d in ("2023-03-15", "2023-03-16", "2023-03-17")]
    for i in gap:
        closes[i] = None  # 停牌三天
    data = make_data({"600701.SH": StockSeries(list(CAL), closes)})
    res = run(make_req([], "2023-03-01", "2023-03-31", cost_bps=0.0), data)
    cm = curve_map(res)
    t0 = IDX["2023-03-01"]
    before = CAL[gap[0] - 1]
    for i in gap:
        assert cm[CAL[i]] == pytest.approx(cm[before], abs=1e-9)
    after = CAL[gap[-1] + 1]
    assert cm[after] == pytest.approx(1.01 ** (IDX[after] - t0), abs=2e-6)  # 复牌后一次性体现
    assert any("沿用前一可用收盘价" in a and "共 3 个持仓·日" in a for a in res.assumptions)


def test_listing_days_by_rebalance_date_and_st_by_current_name():
    new_series = StockSeries(list(CAL), [None if d < "2023-01-10" else 10.0 for d in CAL], turnover=[1e8] * N)
    prices = {"600801.SH": new_series, "600802.SH": flat(), "600803.SH": flat()}
    info = info_for(
        prices,
        **{
            "600801.SH": StockInfo(thscode="600801.SH", ticker="600801", name="示例新股", list_date="2023-01-10"),
            "600802.SH": StockInfo(thscode="600802.SH", ticker="600802", name="*ST示例", list_date="2010-01-04"),
        },
    )
    conds = [cond("sys_st", "is_st", "is_false"), cond("sys_new", "listing_days", "gte", 365)]
    res = run(make_req(conds, "2023-06-01", "2024-02-29"), make_data(prices, info=info))
    h = holdings_by_date(res)
    assert h["2023-06-01"] == ["600803.SH"]
    assert h["2024-01-01"] == ["600803.SH"]  # 合成日历无节假日；2023-01-10 → 2024-01-01 = 356 天
    assert h["2024-02-01"] == ["600801.SH", "600803.SH"]  # 387 天
    assert all("600802.SH" not in v for v in h.values())
    assert any("当前证券简称" in a for a in res.assumptions)


def test_keep_flagged_policy_still_excludes_missing_data():
    items = [inc(2022, "Q2", "2022-08-20", operating_income=100.0), inc(2023, "Q2", "2023-08-20", operating_income=120.0)]
    data = make_data({"600901.SH": flat(), "600902.SH": flat()}, income={"600901.SH": items})
    res = run(make_req([cond("c1", "revenue_yoy", "gt", 0)], "2023-09-01", "2023-09-29", missing_policy="keep_flagged"), data)
    assert res.rebalances[0].holdings == ["600901.SH"]
    assert any("保留并标记" in a for a in res.assumptions)


def test_dataset_status_passthrough_and_partial_warning():
    ds = [
        DatasetStatus(dataset="history", label="历史日K（后复权）", endpoint="/api/a-share/prices/historical",
                      status="partial", requested=3, succeeded=2, failed=1, errors=["code=5003 request_id=demo"]),
        DatasetStatus(dataset="benchmark", label="基准指数日K（沪深300）", endpoint="/api/a-share-index/prices/historical",
                      status="ok", requested=1, succeeded=1),
    ]
    data = make_data({"600001.SH": flat(), "600002.SH": flat()}, members=["600001.SH", "600002.SH", "600003.SH"], datasets=ds)
    res = run(make_req([], "2023-03-01", "2023-03-10", top_n=None), data)
    assert res.datasets == ds
    assert any("部分失败" in a and "不会视为通过" in a for a in res.assumptions)
    assert res.rebalances[0].holdings == ["600001.SH", "600002.SH"]
    assert "1 只满足条件但调仓日前无可用价格" in res.rebalances[0].note


def test_parameter_edge_cases_are_reported():
    closes: list[float | None] = [10.0] * N
    closes[IDX["2023-03-01"]] = None  # 建仓日无收盘价：按沿用价格成交并计数
    prices = {f"60110{i}.SH": flat() for i in range(1, 6)}
    prices["601106.SH"] = StockSeries(list(CAL), closes)
    c1 = cond("c1", "return_20d", "pct_rank_gte", 0, rank_scope="universe")
    req = make_req([c1], "2023-03-01", "2023-03-31", top_n=0, cost_bps=-5.0, report_period="2022-4")
    res = run(req, make_data(prices))
    assert len(res.rebalances[0].holdings) == 6  # top_n=0 视为不限制
    joined = "\n".join(res.assumptions)
    for key in ("top_n=0 无效", "cost_bps 为负", "报告期（2022-4）在回测中不适用", "1 笔调仓成交"):
        assert key in joined, key
    assert res.curve[1].strategy == 1.0  # 成本按 0 计


# ───────────────────────────── 确定性 / 进度 / 合规 / 失败 ─────────────────────────────


def _complex_case(reverse: bool = False) -> tuple[BacktestRequest, BacktestData]:
    prices = _ranking_universe()
    prices.update({f"60300{i}.SH": step_series(IDX["2023-02-01"], 0.01 * i) for i in range(1, 4)})
    income = {"600101.SH": [inc(2022, "Q2", "2022-08-20", operating_income=10.0), inc(2023, "Q2", "2023-08-20", operating_income=12.0)]}
    conds = [
        cond("c1", "return_20d", "pct_rank_gte", 20, rank_scope="universe"),
        cond("c2", "avg_amount_20d", "gte", 0.5, hard=False, weight=2.0),
        cond("c3", "revenue_yoy", "gt", 0, hard=False),
        cond("c4", "roe", "gt", 0),
    ]
    req = make_req(conds, "2023-03-01", "2023-12-29", top_n=3, sort_metric="return_60d", cost_bps=15.0)
    codes = list(prices)
    if reverse:
        codes = codes[::-1]
        prices = {c: prices[c] for c in codes}
    return req, make_data(prices, income=income, members=codes, info=info_for(codes))


def test_deterministic_same_input_same_output():
    req, data = _complex_case()
    a = run(req, data).model_dump(exclude={"created_at", "finished_at"})
    b = run(req, data).model_dump(exclude={"created_at", "finished_at"})
    req2, data2 = _complex_case(reverse=True)
    c = run(req2, data2).model_dump(exclude={"created_at", "finished_at"})
    assert a == b == c
    assert a["status"] == "done" and len(a["rebalances"]) == 10


def test_progress_callback_monotonic_and_finishes():
    calls: list[tuple[float, str]] = []
    req, data = _complex_case()
    run(req, data, progress=lambda f, t: calls.append((f, t)))
    fracs = [f for f, _ in calls]
    assert fracs[0] == 0.0 and fracs[-1] == 1.0
    assert fracs == sorted(fracs)
    assert all(isinstance(t, str) and t for _, t in calls)
    assert any("调仓" in t for _, t in calls)


FORBIDDEN = ["建议买入", "建议卖出", "稳赚", "保本", "必涨", "目标价", "保证收益", "强烈推荐", "预计上涨", "无风险套利"]


def test_caveats_fixed_and_texts_compliant():
    req, data = _complex_case()
    res = run(req, data)
    joined = "\n".join(res.caveats)
    for key in ("幸存者偏差", "当前成分股", "不代表未来", "涨跌停", "停牌", "固定费率"):
        assert key in joined, key
    texts = res.caveats + res.assumptions + [r.note or "" for r in res.rebalances] + [i.reason for i in res.ignored_conditions]
    for t in texts:
        for bad in FORBIDDEN:
            assert bad not in t, (bad, t)


def test_invalid_inputs_return_failed_result():
    data = make_data({"600001.SH": flat()})
    after = run(make_req([], "2025-01-02", None), data)
    assert after.status == "failed" and "晚于交易日历最后一个交易日" in (after.error or "")
    assert after.caveats  # 失败也带警示
    weekend = run(make_req([], "2023-03-04", "2023-03-05"), data)
    assert weekend.status == "failed" and "交易日不足" in (weekend.error or "")
    one_day = run(make_req([], "2023-03-06", "2023-03-06"), data)
    assert one_day.status == "failed" and "交易日不足" in (one_day.error or "")
    reversed_range = run(make_req([], "2023-06-01", "2023-03-01"), data)
    assert reversed_range.status == "failed" and "晚于" in (reversed_range.error or "")
    bad_format = run(make_req([], "2023/03/01", "2023-06-01"), data)
    assert bad_format.status == "failed" and "yyyy-MM-dd" in (bad_format.error or "")
    empty_pool = run(make_req([], "2023-03-01", "2023-06-01"), make_data({}, members=[]))
    assert empty_pool.status == "failed" and "股票池为空" in (empty_pool.error or "")
    no_prices = run(make_req([], "2023-03-01", "2023-06-01"), make_data({}, members=["600001.SH"]))
    assert no_prices.status == "failed" and "历史日K" in (no_prices.error or "")


def test_stock_series_length_mismatch_raises():
    with pytest.raises(ValueError):
        StockSeries(["2023-01-02", "2023-01-03"], [1.0])
    with pytest.raises(ValueError):
        StockSeries(["2023-01-02"], [1.0], close_raw=[1.0, 2.0])


# ───────────────────────────── 纯函数 ─────────────────────────────


def test_window_and_stat_functions():
    assert period_return_pct([10, 12]) == pytest.approx(20.0)
    assert max_drawdown_pct([100, 120, 90, 130, 104]) == pytest.approx(25.0)
    logs = [0.01, -0.01, 0.01, -0.01]
    p = [100 * math.exp(x) for x in np.cumsum([0.0, *logs])]
    assert annualized_volatility(p) == pytest.approx(float(np.std(logs, ddof=1)) * math.sqrt(252) * 100)
    assert annualized_volatility([10, 11]) is None
    rm = [0.01, -0.02, 0.015, 0.003, -0.007, 0.012]
    assert beta_coefficient([2 * x + 0.001 for x in rm], rm) == pytest.approx(2.0)
    assert beta_coefficient([0.1, 0.2, 0.3], [0.01, 0.01, 0.01]) is None

    s = perf_stats([1.0, 1.1, 0.99, 1.2])
    assert s.total_return == pytest.approx(20.0)
    assert s.max_drawdown == pytest.approx(10.0)
    assert s.annual_return == pytest.approx((1.2 ** (252 / 3) - 1) * 100, rel=1e-6)
    flat_stats = perf_stats([1.0, 1.0, 1.0])
    assert flat_stats.annual_vol == 0.0 and flat_stats.sharpe is None
