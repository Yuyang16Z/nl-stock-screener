"""features：已知答案的合成序列验证计算口径；null/错误/不适用的传播；证据字段完整性。"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import pytest

from app.catalog import METRICS
from app.data import features as F
from app.data.features import (
    Fetched,
    RawBundle,
    StockRaw,
    calc_beta,
    calc_max_drawdown,
    calc_volatility,
    compute_features,
    compute_stock_metrics,
    fmt_value,
    revenue_ttm,
    income_items,
)
from app.data.fuyao import FuyaoError, fault_error
from app.data.periods import date_to_ms, make_report_period
from app.schemas import StockInfo

try:
    from _fixtures_data import LAST_TRADE_DAY, TODAY, bars_payload, income_item, indicators_payload, make_resp, trading_days
except ImportError:  # pragma: no cover
    from tests._fixtures_data import LAST_TRADE_DAY, TODAY, bars_payload, income_item, indicators_payload, make_resp, trading_days

CODE = "600001.SH"
REPORT = make_report_period(None, TODAY)  # 2026-2（中报）

EP_VAL = "/api/a-share/valuations/snapshot"
EP_SNAP = "/api/a-share/prices/snapshot"
EP_IND = "/api/a-share/financials/indicators"
EP_INC = "/api/a-share/financials/income-statements"
EP_HIST = "/api/a-share/prices/historical"
EP_IDX = "/api/a-share-index/prices/historical"
EP_DIV = "/api/a-share/corporate-actions/adjustment-factors"
EP_TICK = "/api/meta/tickers/list"


# ───────────────────────────── 构造工具 ─────────────────────────────


def info(name: str = "测试甲", industry: str | None = "测试电子", list_date: str = "2010-01-04") -> StockInfo:
    return StockInfo(thscode=CODE, ticker=CODE[:6], name=name, industry=industry, industry_code="881110.TI" if industry else None, list_date=list_date)


def batch(ds: str, endpoint: str, row: dict[str, Any] | None, rid: str) -> Fetched:
    items = [row] if row is not None else []
    resp = make_resp(endpoint, {"timestamp": date_to_ms(LAST_TRADE_DAY) + 54_000_000, "item": items}, params={"thscodes": f"{CODE},000007.SZ"}, request_id=rid, dataset=ds)
    return Fetched(ds, response=resp, row=row)


def valuations(**fields: Any) -> Fetched:
    row = {"thscode": CODE, "ticker": CODE[:6], "name": "测试甲", "pe_ttm": 15.5, "pe_mrq": 15.0, "pb_mrq": 1.2, "ps_ttm": 2.0, "pcf_ttm": 12.0}
    row.update(fields)
    return batch("valuations", EP_VAL, row, "rid-val")


def snapshot(**fields: Any) -> Fetched:
    row = {"thscode": CODE, "ticker": CODE[:6], "last_price": 50.0, "price_change_ratio_pct": 1.25, "turnover": 3.0e8}
    row.update(fields)
    return batch("snapshot", EP_SNAP, row, "rid-snap")


IND_CUR = {
    "calculate_operating_income_yoy_growth_ratio": "12.5000",
    "calculate_parent_holder_net_profit_yoy_growth_ratio": "8.0",
    "calculate_operating_profit_yoy_growth_ratio": "7.0",
    "index_weighted_avg_roe": "6.0",
    "index_deduct_weighted_avg_roe": "5.5",
    "sale_gross_margin": "35.00000000",
    "sale_net_interest_ratio": "12.0",
    "net_profit_cash_content": "95.0",
    "operating_cash_flow_net_divide_income": "10.0",
    "assets_debt_ratio": "40.0",
    "current_ratio": "1.80",
}
IND_YOY = {**IND_CUR, "index_weighted_avg_roe": "4.5", "sale_gross_margin": "33.0"}


def indicators(values: dict[str, Any], *, yoy: bool = False) -> Fetched:
    ds = "indicators_yoy" if yoy else "indicators"
    report = REPORT.yoy_report if yoy else REPORT.report
    resp = make_resp(EP_IND, indicators_payload(CODE, report, values), params={"thscode": CODE, "report": report}, request_id=f"rid-{ds}", dataset=ds)
    return Fetched(ds, response=resp)


def income(items: list[dict[str, Any]] | None = None) -> Fetched:
    items = items if items is not None else [
        income_item(CODE, 2026, "Q2", 6.0e9, 1.2e9, report_date=date(2026, 8, 20)),
        income_item(CODE, 2026, "Q1", 2.8e9, 0.5e9),
        income_item(CODE, 2025, "Q4", 1.0e10, 2.0e9),
        income_item(CODE, 2025, "Q3", 7.2e9, 1.4e9),
        income_item(CODE, 2025, "Q2", 4.5e9, 0.9e9),
        income_item(CODE, 2025, "Q1", 2.0e9, 0.4e9),
    ]
    resp = make_resp(EP_INC, {"timestamp": items[0]["period_end_ms"] if items else None, "item": items}, params={"thscode": CODE, "period": "quarterly", "limit": 20}, request_id="rid-inc", dataset="income")
    return Fetched("income", response=resp)


def history(closes: list[float], *, end: date = LAST_TRADE_DAY, turnovers: list[float] | None = None) -> Fetched:
    days = trading_days(end, len(closes))
    resp = make_resp(EP_HIST, bars_payload(days, closes, turnovers), params={"thscode": CODE, "adjust": "backward"}, request_id="rid-hist", dataset="history")
    return Fetched("history", response=resp)


def benchmark(closes: list[float], *, end: date = LAST_TRADE_DAY) -> Fetched:
    days = trading_days(end, len(closes))
    resp = make_resp(EP_IDX, bars_payload(days, closes), params={"thscode": "000300.SH"}, request_id="rid-bench", dataset="benchmark")
    return Fetched("benchmark", response=resp)


def dividends(events: list[tuple[date, float, float]] | None = None) -> Fetched:
    events = events if events is not None else [(date(2026, 6, 15), 1.2, 0.0), (date(2025, 12, 1), 0.8, 0.0), (date(2025, 9, 1), 2.0, 0.0), (date(2026, 7, 1), 0.0, 0.3)]
    items = [{"ticker": CODE[:6], "ex_date_ms": date_to_ms(d), "dividend_per_share": c, "per_share_bonus": b} for d, c, b in sorted(events, reverse=True)]
    resp = make_resp(EP_DIV, {"thscode": CODE, "ticker": CODE[:6], "item": items}, params={"thscode": CODE, "from": "2025-09-12"}, request_id="rid-div", dataset="dividends")
    return Fetched("dividends", response=resp)


def tickers(name: str = "测试甲", list_date: str | None = "2025-09-12") -> Fetched:
    row = {"thscode": CODE, "ticker": CODE[:6], "name": name, "exchange": "SH", "list_date": list_date}
    resp = make_resp(EP_TICK, {"timestamp": date_to_ms(TODAY), "item": [row]}, params={"asset_type": "a-share", "limit": 10000, "offset": 0}, request_id="rid-tick", dataset="tickers")
    return Fetched("tickers", response=resp, row=row)


def failed(ds: str, *, code: int = 5003, kind: str = "upstream_unavailable", rid: str = "rid-err") -> Fetched:
    return Fetched(ds, error=FuyaoError("上游不可用", kind=kind, code=code, request_id=rid, endpoint=EP_IND, dataset=ds, params={"thscode": CODE}))  # type: ignore[arg-type]


def geometric(n: int, step: float = 1.001, start: float = 100.0) -> list[float]:
    return [start * step**t for t in range(n)]


def beta_series(n: int = 261) -> tuple[list[float], list[float]]:
    rm = [0.01 * math.sin(0.7 * t) + 0.002 * math.cos(1.3 * t) for t in range(1, n)]
    ri = [0.0003 + 1.5 * r for r in rm]
    pm, pi = [3000.0], [50.0]
    for a, b in zip(rm, ri):
        pm.append(pm[-1] * (1 + a))
        pi.append(pi[-1] * (1 + b))
    return pi, pm


def full_stock(**overrides: Fetched) -> StockRaw:
    stock_closes, _ = beta_series()
    ds: dict[str, Fetched] = {
        "valuations": valuations(),
        "snapshot": snapshot(),
        "indicators": indicators(IND_CUR),
        "indicators_yoy": indicators(IND_YOY, yoy=True),
        "income": income(),
        "history": history(stock_closes, turnovers=[float(i % 20 + 1) * 1e8 for i in range(len(stock_closes))]),
        "dividends": dividends(),
        "tickers": tickers(),
    }
    ds.update(overrides)
    return StockRaw(info=info(), datasets=ds)


def run(stock: StockRaw, metrics: list[str] | set[str], *, bench: Fetched | None = None, trade_date: date | None = None) -> dict[str, Any]:
    return compute_stock_metrics(stock, metrics, REPORT, TODAY, benchmark=bench, trade_date=trade_date)


# ───────────────────────────── 价格窗口（已知答案） ─────────────────────────────


def test_volatility_alternating_returns() -> None:
    a = 0.01
    closes = [100.0]
    for t in range(259):
        closes.append(closes[-1] * math.exp(a if t % 2 == 0 else -a))
    mv = run(StockRaw(info(), {"history": history(closes)}), ["volatility_250d"])["volatility_250d"]
    expected = a * math.sqrt(250 / 249) * math.sqrt(252) * 100  # 样本标准差
    assert mv.status == "ok"
    assert mv.value == pytest.approx(expected, rel=1e-9)
    assert mv.evidence.period == f"近250个交易日（截至{LAST_TRADE_DAY.isoformat()}）"
    assert mv.evidence.derived is True and mv.evidence.formula
    assert mv.evidence.unit == "%" and mv.evidence.display == fmt_value(expected, "%")


def test_max_drawdown_known_answer() -> None:
    up = [100 + 20 * i / 99 for i in range(100)]
    down = [120 - 30 * (i + 1) / 50 for i in range(50)]
    rec = [90 + 20 * (i + 1) / 101 for i in range(101)]
    closes = up + down + rec
    assert len(closes) == 251
    mv = run(StockRaw(info(), {"history": history(closes)}), ["max_drawdown_250d"])["max_drawdown_250d"]
    assert mv.status == "ok"
    assert mv.value == pytest.approx(25.0, abs=1e-9)
    names = [e.metric for e in mv.evidence.inputs]
    assert "close_price（前高）" in names and "close_price（回撤低点）" in names
    peak = next(e for e in mv.evidence.inputs if e.metric == "close_price（前高）")
    trough = next(e for e in mv.evidence.inputs if e.metric == "close_price（回撤低点）")
    assert peak.value == pytest.approx(120.0) and trough.value == pytest.approx(90.0)
    assert calc_max_drawdown([1.0, 2.0, 3.0]) == (0.0, 0, 0)


def test_period_returns_geometric_series() -> None:
    closes = geometric(300)
    out = run(StockRaw(info(), {"history": history(closes)}), ["return_20d", "return_60d", "return_250d"])
    for mid, n in (("return_20d", 20), ("return_60d", 60), ("return_250d", 250)):
        assert out[mid].status == "ok"
        assert out[mid].value == pytest.approx((1.001**n - 1) * 100, rel=1e-9)
        assert out[mid].evidence.inputs[0].metric == "close_price（区间起点前一日）"


def test_beta_known_answer_and_alignment() -> None:
    stock, market = beta_series()
    out = run(StockRaw(info(), {"history": history(stock)}), ["beta_250d"], bench=benchmark(market))
    mv = out["beta_250d"]
    assert mv.status == "ok"
    assert mv.value == pytest.approx(1.5, abs=1e-9)
    assert {s.endpoint for s in mv.evidence.sources} == {EP_HIST, EP_IDX}
    # 基准缺失若干日期：按日期对齐后仍 ≥ 0.8N 时照常计算
    short_market = market[:]  # 同日期
    days = trading_days(LAST_TRADE_DAY, len(short_market))
    payload = bars_payload(days, short_market)
    payload["item"] = [it for i, it in enumerate(payload["item"]) if i % 10 != 5]
    gappy = Fetched("benchmark", response=make_resp(EP_IDX, payload, request_id="rid-bench2", dataset="benchmark"))
    mv2 = run(StockRaw(info(), {"history": history(stock)}), ["beta_250d"], bench=gappy)["beta_250d"]
    assert mv2.status == "ok"
    assert mv2.value == pytest.approx(1.5, rel=0.05)
    assert calc_beta([0.01, 0.02, 0.03], [0.0, 0.0, 0.0]) is None


def test_beta_benchmark_failure_propagates_error() -> None:
    stock, _ = beta_series()
    mv = run(StockRaw(info(), {"history": history(stock)}), ["beta_250d"], bench=failed("benchmark", rid="rid-b"))["beta_250d"]
    assert mv.status == "error"
    assert "rid-b" in (mv.reason or "") and "code=5003" in (mv.reason or "")


def test_avg_amount_20d() -> None:
    closes = geometric(30)
    turnovers = [5.0e8] * 10 + [float(i + 1) * 1e8 for i in range(20)]
    mv = run(StockRaw(info(), {"history": history(closes, turnovers=turnovers)}), ["avg_amount_20d"])["avg_amount_20d"]
    assert mv.status == "ok"
    assert mv.value == pytest.approx(10.5)
    assert mv.evidence.display == "10.50亿元"
    assert mv.evidence.sources[0].field == "turnover"


def test_insufficient_samples_are_missing() -> None:
    closes = geometric(150)
    out = run(StockRaw(info(), {"history": history(closes)}), ["volatility_250d", "max_drawdown_250d", "beta_250d", "return_20d"], bench=benchmark(geometric(150)))
    for mid in ("volatility_250d", "max_drawdown_250d", "beta_250d"):
        assert out[mid].status == "missing"
        assert "样本不足" in (out[mid].reason or "")
        assert "250×0.8=200" in (out[mid].reason or "")
    assert out["return_20d"].status == "ok"  # 短窗口样本足够


def test_shortfall_within_tolerance_uses_available_window() -> None:
    closes = geometric(221)  # 220 个区间 ≥ 200
    mv = run(StockRaw(info(), {"history": history(closes)}), ["return_250d"])["return_250d"]
    assert mv.status == "ok"
    assert mv.value == pytest.approx((1.001**220 - 1) * 100, rel=1e-9)
    assert "不足250个" in (mv.evidence.period or "")


def test_suspended_stock_is_flagged_stale() -> None:
    closes = geometric(300)
    mv = run(StockRaw(info(), {"history": history(closes, end=date(2026, 10, 9))}), ["return_20d"], trade_date=LAST_TRADE_DAY)["return_20d"]
    assert mv.status == "ok"
    assert mv.stale is True
    assert "可能停牌" in (mv.evidence.note or "")


def test_zero_volume_or_turnover_bars_are_excluded() -> None:
    closes = geometric(40)
    days = trading_days(LAST_TRADE_DAY, 40)
    payload = bars_payload(days, closes, volumes=[0.0 if i == 39 else 1e6 for i in range(40)])
    fetched = Fetched("history", response=make_resp(EP_HIST, payload, request_id="rid-h", dataset="history"))
    mv = run(StockRaw(info(), {"history": fetched}), ["return_20d"])["return_20d"]
    assert mv.value == pytest.approx((closes[38] / closes[18] - 1) * 100, rel=1e-9)
    payload2 = bars_payload(days, closes, turnovers=[0.0 if i == 39 else 2e8 for i in range(40)])
    fetched2 = Fetched("history", response=make_resp(EP_HIST, payload2, request_id="rid-h2", dataset="history"))
    mv2 = run(StockRaw(info(), {"history": fetched2}), ["return_20d"])["return_20d"]
    assert mv2.value == pytest.approx(mv.value, rel=1e-12)  # 与回测口径一致：零成交额视为停牌


def test_bar_date_matches_ms_to_date_and_parse_is_cached() -> None:
    from datetime import timedelta

    from app.data.periods import ms_to_date

    d = date(2022, 1, 1)
    while d <= date(2026, 12, 31):
        ms = date_to_ms(d)
        assert F.bar_date(ms) == ms_to_date(ms) == d
        assert F.bar_date(ms + 15 * 3_600_000) == d  # 当日 15:00
        d += timedelta(days=37)
    fetched = history(geometric(30))
    first = F.fetched_bars(fetched)
    assert F.fetched_bars(fetched) is first  # 共享序列只解析一次
    assert F.latest_bar_date(fetched) == LAST_TRADE_DAY


def test_market_trade_date_is_mode_with_late_tiebreak() -> None:
    d1, d2, d3 = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)
    assert F.market_trade_date([d3, d3, d2, None, d3]) == d3
    assert F.market_trade_date([d2, d2, d3]) == d2  # 个别序列更晚不改变市场交易日
    assert F.market_trade_date([d1, d2]) == d2  # 并列取较晚者
    assert F.market_trade_date([None]) is None


# ───────────────────────────── 财务指标 ─────────────────────────────


def test_roe_change_and_annualized() -> None:
    stock = full_stock()
    out = run(stock, ["roe", "roe_annualized", "roe_change", "gross_margin_change", "revenue_yoy", "current_ratio"])
    assert out["roe"].value == pytest.approx(6.0)
    assert out["roe"].evidence.note == "报告期累计值（未年化）"
    assert out["roe_annualized"].value == pytest.approx(12.0)  # 6.0 × 4 ÷ 2
    assert "年化估算" in (out["roe_annualized"].evidence.note or "")
    assert out["roe_change"].value == pytest.approx(1.5)  # 6.0 − 4.5
    assert out["roe_change"].evidence.display == "+1.50个百分点"
    assert out["gross_margin_change"].value == pytest.approx(2.0)
    assert out["revenue_yoy"].value == pytest.approx(12.5)  # 字符串 → float
    assert out["current_ratio"].evidence.display == "1.80倍"
    rc = out["roe_change"].evidence
    assert rc.derived and len(rc.inputs) == 2
    assert {s.request_id for s in rc.sources} == {"rid-indicators", "rid-indicators_yoy"}
    assert rc.inputs[1].period == "2025年中报（2025-2）"


def test_roe_annualized_for_q3_report() -> None:
    rep3 = make_report_period("2026-3", TODAY)
    resp = make_resp(EP_IND, indicators_payload(CODE, "2026-3", {"index_weighted_avg_roe": "9.0"}), request_id="r", dataset="indicators")
    out = compute_stock_metrics(StockRaw(info(), {"indicators": Fetched("indicators", response=resp)}), ["roe_annualized"], rep3, TODAY)
    assert out["roe_annualized"].value == pytest.approx(12.0)  # 9.0 × 4 ÷ 3


def test_null_indicator_is_missing_with_reason() -> None:
    stock = full_stock(indicators=indicators({**IND_CUR, "calculate_parent_holder_net_profit_yoy_growth_ratio": None}))
    mv = run(stock, ["net_profit_yoy"])["net_profit_yoy"]
    assert mv.status == "missing"
    assert mv.reason == F.NULL_REASON
    assert mv.value is None and mv.evidence.display == "--"
    assert mv.evidence.sources[0].field == "calculate_parent_holder_net_profit_yoy_growth_ratio"
    assert mv.evidence.sources[0].request_id == "rid-indicators"


def test_unparseable_and_absent_indicator_are_missing() -> None:
    values = {k: v for k, v in IND_CUR.items() if k != "assets_debt_ratio"}
    values["calculate_operating_income_yoy_growth_ratio"] = "abc"
    out = run(full_stock(indicators=indicators(values)), ["revenue_yoy", "debt_ratio"])
    assert out["revenue_yoy"].status == "missing" and "无法解析" in (out["revenue_yoy"].reason or "")
    assert out["debt_ratio"].status == "missing" and "接口未返回字段" in (out["debt_ratio"].reason or "")


def test_dataset_error_propagates_with_code_and_request_id() -> None:
    stock = full_stock(indicators=failed("indicators", rid="rid-x"))
    out = run(stock, ["revenue_yoy", "roe_annualized", "roe_change"])
    for mid in ("revenue_yoy", "roe_annualized", "roe_change"):
        assert out[mid].status == "error", mid
        assert "code=5003" in (out[mid].reason or "")
        assert "request_id=rid-x" in (out[mid].reason or "")
        assert out[mid].value is None


def test_yoy_dataset_error_only_affects_change_metrics() -> None:
    stock = full_stock(indicators_yoy=failed("indicators_yoy", rid="rid-y"))
    out = run(stock, ["roe", "roe_change"])
    assert out["roe"].status == "ok"
    assert out["roe_change"].status == "error"
    assert out["roe_change"].reason.startswith("上年同期ROE：")


def test_not_ready_is_missing_and_fault_injection_is_error() -> None:
    out = run(full_stock(indicators=failed("indicators", code=3002, kind="not_ready")), ["revenue_yoy"])
    assert out["revenue_yoy"].status == "missing" and "code=3002" in (out["revenue_yoy"].reason or "")
    injected = Fetched("indicators", error=fault_error("indicators", EP_IND, {"thscode": CODE}))
    out2 = run(full_stock(indicators=injected), ["revenue_yoy"])
    assert out2["revenue_yoy"].status == "error"
    assert "演示故障注入" in (out2["revenue_yoy"].reason or "")


def test_financial_industry_gross_margin_not_applicable() -> None:
    bank_values = {**IND_CUR, "sale_gross_margin": None, "current_ratio": None}
    bank = StockRaw(
        info=StockInfo(thscode=CODE, ticker=CODE[:6], name="测试乙", industry="测试银行"),
        datasets={"indicators": indicators(bank_values), "indicators_yoy": indicators({**bank_values}, yoy=True)},
    )
    out = run(bank, ["gross_margin", "current_ratio", "gross_margin_change"])
    for mid in ("gross_margin", "current_ratio", "gross_margin_change"):
        assert out[mid].status == "not_applicable", mid
        assert "金融企业" in (out[mid].reason or "")
        assert out[mid].evidence.display == "不适用"
    # 非金融企业同样为空 → missing（不能被当作不适用而放过）
    plain = StockRaw(info=info(industry="测试电子"), datasets={"indicators": indicators(bank_values)})
    out2 = run(plain, ["gross_margin"])
    assert out2["gross_margin"].status == "missing"
    # 行业未知时按简称兜底
    by_name = StockRaw(info=StockInfo(thscode=CODE, ticker=CODE[:6], name="测试证券", industry=None), datasets={"indicators": indicators(bank_values)})
    out3 = run(by_name, ["gross_margin"])
    assert out3["gross_margin"].status == "not_applicable" and "简称" in (out3["gross_margin"].reason or "")


# ───────────────────────────── 利润表 / 市值 / 股息 ─────────────────────────────


def test_revenue_ttm_and_market_cap_ytd() -> None:
    out = run(full_stock(), ["market_cap_est", "revenue_amount", "parent_np_amount"])
    mc = out["market_cap_est"]
    assert mc.status == "ok"
    # 主公式：归母净利润TTM = 12亿 + 20亿 − 9亿 = 23亿；PE 15.5 → 356.5亿
    assert mc.value == pytest.approx(356.5)
    assert mc.evidence.display == "356.5亿元"
    ttm_ev = next(e for e in mc.evidence.inputs if e.metric == "parent_np_ttm")
    assert ttm_ev.value == pytest.approx(23.0)
    assert ttm_ev.formula == "归母净利润TTM = 本期累计 + 上年年报 − 上年同期累计"
    assert len(ttm_ev.inputs) == 3
    # PE 非正时退回市销率法：营收TTM = 60亿 + 100亿 − 45亿 = 115亿；PS 2.0 → 230亿
    out_ps = run(full_stock(valuations=valuations(pe_ttm=-3.0)), ["market_cap_est"])
    assert out_ps["market_cap_est"].value == pytest.approx(230.0)
    assert "市销率法" in (out_ps["market_cap_est"].evidence.formula or "")
    assert out["revenue_amount"].value == pytest.approx(60.0)
    assert out["revenue_amount"].evidence.display == "60.00亿元"
    assert out["parent_np_amount"].value == pytest.approx(12.0)
    assert out["revenue_amount"].evidence.period == "2026年中报（2026-2）"


def test_revenue_ttm_single_quarter_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(F, "INCOME_IS_YTD", False)
    items = [
        income_item(CODE, 2026, "Q2", 3.0e9),
        income_item(CODE, 2026, "Q1", 2.8e9),
        income_item(CODE, 2025, "Q4", 2.7e9),
        income_item(CODE, 2025, "Q3", 2.5e9),
        income_item(CODE, 2025, "Q2", 2.0e9),
    ]
    out = run(full_stock(income=income(items)), ["market_cap_est"])
    assert out["market_cap_est"].value == pytest.approx(220.0)  # (30+28+27+25)亿 × 2
    ttm_ev = next(e for e in out["market_cap_est"].evidence.inputs if e.metric == "revenue_ttm")
    assert ttm_ev.formula == "营业收入TTM = 最近四个单季营业收入之和"


def test_revenue_ttm_annual_anchor_and_missing_inputs() -> None:
    items = income_items({"item": [income_item(CODE, 2025, "FY", 9.0e9), income_item(CODE, 2025, "Q3", 6.0e9)]})
    res = revenue_ttm(items, as_of=TODAY, is_ytd=True)
    assert not isinstance(res, str) and res.value == pytest.approx(9.0e9) and res.anchor == (2025, 4)
    lacking = income_items({"item": [income_item(CODE, 2026, "Q2", 6.0e9)]})
    msg = revenue_ttm(lacking, as_of=TODAY, is_ytd=True)
    assert isinstance(msg, str) and "上年年报" in msg
    # 披露日晚于 as_of 的报告不参与（防前视）
    future = income_items({"item": [income_item(CODE, 2026, "Q2", 6.0e9, report_date=date(2026, 12, 1)), income_item(CODE, 2025, "Q4", 1.0e10)]})
    res2 = revenue_ttm(future, as_of=TODAY, is_ytd=True)
    assert not isinstance(res2, str) and res2.anchor == (2025, 4)


def test_market_cap_not_applicable_when_ps_non_positive() -> None:
    out = run(full_stock(valuations=valuations(pe_ttm=-2.0, ps_ttm=-1.0)), ["market_cap_est"])
    assert out["market_cap_est"].status == "not_applicable"
    assert "非正" in (out["market_cap_est"].reason or "")
    out2 = run(full_stock(valuations=valuations(pe_ttm=None, ps_ttm=None)), ["market_cap_est"])
    assert out2["market_cap_est"].status == "missing"


def test_dividend_yield_ttm() -> None:
    out = run(full_stock(), ["dividend_yield_ttm"])
    mv = out["dividend_yield_ttm"]
    assert mv.status == "ok"
    # 窗口 (2025-10-17, 2026-10-17]：1.2 + 0.8 + 0（送股不计）= 2.0；2025-09-01 的 2.0 在窗口外
    assert mv.value == pytest.approx(4.0)  # 2.0 ÷ 50 × 100
    div_ev = next(e for e in mv.evidence.inputs if e.metric == "dividend_per_share_ttm")
    assert div_ev.value == pytest.approx(2.0)
    no_div = run(full_stock(dividends=dividends([])), ["dividend_yield_ttm"])["dividend_yield_ttm"]
    assert no_div.status == "ok" and no_div.value == 0.0
    assert "无现金分红" in (next(e for e in no_div.evidence.inputs if e.metric == "dividend_per_share_ttm").note or "")
    no_price = run(full_stock(snapshot=snapshot(last_price=None)), ["dividend_yield_ttm"])["dividend_yield_ttm"]
    assert no_price.status == "missing" and "最新价" in (no_price.reason or "")


# ───────────────────────────── 估值 / 快照 / 代码表 ─────────────────────────────


def test_valuation_and_snapshot_direct_fields() -> None:
    out = run(full_stock(valuations=valuations(pe_ttm=21.3567, pb_mrq=None)), ["pe_ttm", "pb_mrq", "pct_change_today"])
    assert out["pe_ttm"].value == pytest.approx(21.3567)
    assert out["pe_ttm"].evidence.display == "21.36倍"
    src = out["pe_ttm"].evidence.sources[0]
    assert src.endpoint == EP_VAL and src.field == "pe_ttm" and src.request_id == "rid-val"
    assert src.params == {"thscodes": CODE, "batch_size": 2}
    assert out["pb_mrq"].status == "missing" and out["pb_mrq"].reason == F.NULL_REASON
    assert out["pct_change_today"].value == pytest.approx(1.25)
    assert out["pct_change_today"].evidence.period.startswith("实时快照")
    # 批量接口未返回该股记录
    absent = run(full_stock(valuations=batch("valuations", EP_VAL, None, "rid-v2")), ["pe_ttm"])["pe_ttm"]
    assert absent.status == "missing" and "未返回该股票记录" in (absent.reason or "")


def test_negative_pe_is_ok_value_not_filtered() -> None:
    mv = run(full_stock(valuations=valuations(pe_ttm=-8.5)), ["pe_ttm"])["pe_ttm"]
    assert mv.status == "ok" and mv.value == pytest.approx(-8.5)  # 正值过滤交给引擎


def test_is_st_and_listing_days() -> None:
    out = run(full_stock(tickers=tickers(name="*ST测试", list_date="2025-09-12")), ["is_st", "listing_days"])
    assert out["is_st"].status == "ok" and out["is_st"].value is True
    assert out["is_st"].evidence.display == "是"
    assert out["listing_days"].value == pytest.approx(400.0)  # 2025-09-12 → 2026-10-17
    assert out["listing_days"].evidence.display == "400天"
    assert out["listing_days"].evidence.inputs[0].value == "2025-09-12"
    normal = run(full_stock(tickers=tickers(name="测试甲")), ["is_st"])["is_st"]
    assert normal.value is False and normal.evidence.display == "否"
    no_date = run(full_stock(tickers=tickers(list_date=None)), ["listing_days"])["listing_days"]
    assert no_date.status == "missing"
    absent = Fetched("tickers", response=tickers().response, row=None)
    assert run(full_stock(tickers=absent), ["is_st"])["is_st"].status == "missing"
    assert run(full_stock(tickers=failed("tickers")), ["is_st"])["is_st"].status == "error"


# ───────────────────────────── 全量与证据完整性 ─────────────────────────────


def test_all_32_metrics_ok_with_complete_evidence() -> None:
    _, market = beta_series()
    out = run(full_stock(), list(METRICS), bench=benchmark(market), trade_date=LAST_TRADE_DAY)
    assert len(METRICS) == 32 and set(out) == set(METRICS)
    for mid, mv in out.items():
        md = METRICS[mid]
        assert mv.status == "ok", (mid, mv.reason)
        ev = mv.evidence
        assert ev is not None and ev.metric == mid
        assert ev.unit == md.unit, mid
        assert ev.display and ev.display != "--", mid
        assert ev.period, mid
        assert ev.as_of is not None, mid
        assert ev.sources, mid
        for s in ev.sources:
            assert s.provider == "fuyao" and s.endpoint and s.endpoint.startswith("/api/"), mid
            assert s.request_id and s.fetched_at is not None, mid
        if md.derived:
            assert ev.derived is True and ev.formula and ev.inputs, mid
        else:
            assert ev.derived is False, mid
        assert mv.stale is False


def test_compute_features_is_deterministic() -> None:
    _, market = beta_series()
    bundle = RawBundle(stocks={CODE: full_stock()}, benchmark=benchmark(market), trade_date=LAST_TRADE_DAY)
    a = compute_features(bundle, set(METRICS) | {"unknown_metric"}, REPORT, TODAY)
    b = compute_features(bundle, set(METRICS), REPORT, TODAY)
    assert a[CODE].model_dump() == b[CODE].model_dump()
    assert "unknown_metric" not in a[CODE].metrics
    assert a[CODE].info.thscode == CODE


def test_missing_dataset_never_ok() -> None:
    out = run(StockRaw(info(), {}), list(METRICS))
    assert all(mv.status != "ok" for mv in out.values())
    assert all(mv.reason for mv in out.values())


def test_catalog_sources_match_field_mapping() -> None:
    for mapping in (F.INDICATOR_FIELDS, F.VALUATION_FIELDS, F.SNAPSHOT_FIELDS, F.INCOME_FIELDS):
        for mid, fld in mapping.items():
            assert fld in METRICS[mid].source, (mid, fld)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (12.3, "%", "12.30%"),
        (21.3567, "倍", "21.36倍"),
        (523.4, "亿元", "523.4亿元"),
        (0.854, "亿元", "0.85亿元"),
        (365, "天", "365天"),
        (0.85, "", "0.85"),
        (True, "", "是"),
        (False, "", "否"),
        (None, "%", "--"),
        (float("nan"), "%", "--"),
        (-0.001, "%", "0.00%"),
        (1.5, "百分点", "+1.50个百分点"),
        (-0.5, "百分点", "-0.50个百分点"),
        (0.035, "元/股", "0.035元/股"),
        (6.0e9, "元", "60.00亿元"),
    ],
)
def test_fmt_value(value: Any, unit: str, expected: str) -> None:
    assert fmt_value(value, unit) == expected


def test_to_float_and_volatility_helpers() -> None:
    assert F.to_float("89.12000000") == pytest.approx(89.12)
    assert F.to_float("-16.0031") == pytest.approx(-16.0031)
    assert F.to_float("1,234.5") == pytest.approx(1234.5)
    assert F.to_float("--") is None and F.to_float(None) is None and F.to_float(True) is None
    assert F.to_float("inf") is None
    assert calc_volatility([1.0, 1.0]) is None
