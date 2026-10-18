"""repository：用假客户端返回合成信封，验证按依赖取数、数据集状态、故障注入、参考组与增量合并。"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from app.catalog import DATASETS, DISPLAY_METRICS
from app.data.cache import ApiCache
from app.data.fuyao import FuyaoClient, FuyaoError
from app.data.repository import INDUSTRY_PICK_RULE, Repository, merge_dataset_status, pick_industry
from app.engine.evaluate import evaluate
from app.schemas import Condition, DatasetStatus, Strategy, Universe

try:
    from _fixtures_data import HS300_MEMBERS, LAST_TRADE_DAY, PE, TODAY, FakeClient, SyntheticMarket, fail_when, make_settings
except ImportError:  # pragma: no cover
    from tests._fixtures_data import HS300_MEMBERS, LAST_TRADE_DAY, PE, TODAY, FakeClient, SyntheticMarket, fail_when, make_settings


def make_repo(market: SyntheticMarket | None = None) -> tuple[Repository, FakeClient, SyntheticMarket]:
    market = market or SyntheticMarket()
    client = FakeClient(market)
    repo = Repository(client, make_settings(), today=lambda: TODAY)  # type: ignore[arg-type]
    return repo, client, market


def strategy(*conds: Condition, universe: Universe | None = None, report: str | None = None) -> Strategy:
    return Strategy(universe=universe or Universe(), conditions=list(conds), report_period=report)


BASIC = (
    Condition(id="c1", metric="revenue_yoy", op="gt", value=0),
    Condition(id="c2", metric="roe_change", op="gt", value=0),
    Condition(id="c3", metric="volatility_250d", op="pct_rank_lte", value=50, rank_scope="universe"),
    Condition(id="sys_st", metric="is_st", op="is_false"),
    Condition(id="sys_new", metric="listing_days", op="gte", value=365),
    Condition(id="off", metric="dividend_yield_ttm", op="gte", value=3, enabled=False),
)


def ds_map(snap: Any) -> dict[str, DatasetStatus]:
    return {d.dataset: d for d in snap.datasets}


# ───────────────────────────── build_snapshot ─────────────────────────────


async def test_build_snapshot_fetches_only_needed_datasets() -> None:
    repo, client, _ = make_repo()
    progress: list[tuple[float, str]] = []
    snap = await repo.build_snapshot(strategy(*BASIC), faults=set(), progress=lambda f, t: progress.append((f, t)))

    assert sorted(snap.stocks) == sorted(HS300_MEMBERS)
    assert snap.universe.code == "000300.SH" and snap.universe.size == 4
    assert snap.report_period.report == "2026-2"
    assert snap.trade_date == LAST_TRADE_DAY.isoformat()
    expected_metrics = {"revenue_yoy", "roe_change", "volatility_250d", "is_st", "listing_days", *DISPLAY_METRICS}
    assert set(snap.metrics_available) == expected_metrics
    for sf in snap.stocks.values():
        assert set(sf.metrics) == expected_metrics  # 未启用条件（股息率）不取数

    st = ds_map(snap)
    assert [d.dataset for d in snap.datasets] == list(DATASETS)
    # 估值类指标会顺带取行情快照（每 100 只 1 次），用于识别停牌（成交量为 0）
    for name in ("constituents", "tickers", "industry_map", "valuations", "snapshot", "indicators", "indicators_yoy", "history"):
        assert st[name].status == "ok", name
    assert client.count("snapshot") == 1
    for name in ("income", "benchmark", "dividends"):
        assert st[name].status == "skipped", name
        assert client.count(name) == 0
    assert st["indicators"].requested == 4 and st["indicators"].succeeded == 4
    assert st["valuations"].requested == 4 and client.count("valuations") == 1  # 一批 100
    assert st["history"].as_of is not None and st["history"].fetched_at is not None
    assert st["indicators"].label == DATASETS["indicators"][0]
    assert "二级行业" in (st["industry_map"].note or "")

    # 行业归属（成员数 ≥ 8 的最细一级；银行只在 1 只成员的指数里 → 取成员最多者）
    assert snap.stocks["600001.SH"].info.industry == "测试电子"
    assert snap.stocks["600001.SH"].info.industry_code == "881110.TI"
    assert snap.stocks["300004.SZ"].info.industry == "测试电子"
    assert snap.stocks["000003.SZ"].info.industry == "测试制造"
    assert snap.stocks["600002.SH"].info.industry == "测试银行"
    assert snap.stocks["600001.SH"].info.name == "测试甲"
    assert snap.stocks["600001.SH"].info.list_date == "2010-01-04"

    m = snap.stocks
    assert m["600001.SH"].metrics["revenue_yoy"].value == pytest.approx(12.5)
    assert m["600001.SH"].metrics["roe_change"].value == pytest.approx(1.0)
    assert m["000003.SZ"].metrics["is_st"].value is True
    assert m["300004.SZ"].metrics["listing_days"].value == pytest.approx((TODAY - date(2026, 3, 2)).days)
    assert m["600001.SH"].metrics["volatility_250d"].status == "ok"
    assert m["000003.SZ"].metrics["pe_ttm"].value == pytest.approx(-12.0)
    # 历史 K 线用后复权、统一窗口；股票池只请求一次估值批次
    hist_params = [p for path, p, ds in client.calls if ds == "history"]
    assert all(p["adjust"] == "backward" and p["interval"] == "1d" for p in hist_params)
    assert len({(p["start"], p["end"]) for p in hist_params}) == 1
    yoy_params = [p for path, p, ds in client.calls if ds == "indicators_yoy"]
    assert {p["report"] for p in yoy_params} == {"2025-2"}

    # 进度：单调不减、以 1.0 结束、文本含数据集与计数
    fracs = [f for f, _ in progress]
    assert fracs == sorted(fracs) and fracs[-1] == 1.0
    assert any(t.startswith("取财务指标（本期） ") and "/4" in t for _, t in progress)
    assert snap.reference == {} and snap.reference_industry == {}


async def test_partial_failure_marks_dataset_partial_and_metric_error() -> None:
    market = SyntheticMarket()
    market.fail_rules.append(fail_when("indicators", "600002.SH", request_id="rid-fail"))
    repo, _, _ = make_repo(market)
    snap = await repo.build_snapshot(strategy(*BASIC), faults=set())
    st = ds_map(snap)["indicators"]
    assert st.status == "partial"
    assert (st.requested, st.succeeded, st.failed) == (4, 3, 1)
    assert st.errors == ["600002.SH: code=5003 上游不可用 (request_id=rid-fail)"]
    bank = snap.stocks["600002.SH"].metrics
    assert bank["revenue_yoy"].status == "error"
    assert "rid-fail" in (bank["revenue_yoy"].reason or "")
    assert bank["roe_change"].status == "error"
    assert snap.stocks["600001.SH"].metrics["revenue_yoy"].status == "ok"
    assert ds_map(snap)["indicators_yoy"].status == "ok"


async def test_fault_injection_fails_dataset_without_hiding() -> None:
    repo, client, _ = make_repo()
    snap = await repo.build_snapshot(strategy(*BASIC), faults={"valuations"})
    st = ds_map(snap)
    assert st["valuations"].status == "failed"
    assert st["valuations"].failed == 4 and st["valuations"].succeeded == 0
    assert "演示故障注入" in st["valuations"].errors[0]
    assert st["valuations"].errors[0].startswith("000003.SZ 等 4 只: code=5003")
    for sf in snap.stocks.values():
        pe = sf.metrics["pe_ttm"]
        assert pe.status == "error" and "演示故障注入" in (pe.reason or "")
        assert sf.metrics["revenue_yoy"].status == "ok"  # 其他数据集不受影响
    assert st["indicators"].status == "ok"
    assert client.count("valuations") == 1


async def test_industry_and_market_reference_groups() -> None:
    conds = (
        Condition(id="c1", metric="pe_ttm", op="gt", value=0),
        Condition(id="c2", metric="pe_ttm", op="pct_rank_lte", value=50, rank_scope="industry"),
        Condition(id="c3", metric="pb_mrq", op="pct_rank_lte", value=50, rank_scope="market"),
    )
    repo, client, _ = make_repo()
    snap = await repo.build_snapshot(strategy(*conds), faults=set())
    ref = snap.reference["pe_ttm"]
    expected = {c: v for c, v in PE.items() if v is not None and c != "600005.SH"}  # 名称含“退”的不入参考组
    assert ref == pytest.approx(expected)
    assert ref["600010.SH"] == pytest.approx(-3.0)  # 负值保留，正值过滤交给引擎
    assert "000008.SZ" not in ref  # null 不入参考组
    assert set(snap.reference["pb_mrq"]) == set(expected) | {"000008.SZ"}
    assert snap.reference_industry["600001.SH"] == "881110.TI"
    assert snap.reference_industry["600002.SH"] == "881200.TI"
    assert snap.reference_industry["600012.SH"] == "881100.TI"
    assert snap.industry_names["881110.TI"] == "测试电子"
    st = ds_map(snap)
    assert st["valuations"].requested == 4  # 状态按股票池成员计数
    assert "全A参考组：11/11" in (st["valuations"].note or "")
    assert client.count("valuations") == 1  # 股票池与全A参考组合并为一次批量
    assert st["industry_map"].status == "ok"


async def test_incremental_base_merge() -> None:
    repo, client, _ = make_repo()
    s = strategy(*BASIC)
    base = await repo.build_snapshot(s, faults=set(), metrics={"pe_ttm"})
    assert set(base.stocks["600001.SH"].metrics) == {"pe_ttm"}
    calls_before = client.count("valuations")
    merged = await repo.build_snapshot(s, faults=set(), metrics={"pe_ttm", "revenue_yoy"}, base=base)
    assert merged.snapshot_id != base.snapshot_id
    for code in HS300_MEMBERS:
        assert set(merged.stocks[code].metrics) == {"pe_ttm", "revenue_yoy"}
        assert merged.stocks[code].metrics["pe_ttm"] == base.stocks[code].metrics["pe_ttm"]  # 保留 base 数据
    assert client.count("valuations") == calls_before  # 已有指标不重取
    st = ds_map(merged)
    assert st["valuations"].status == "ok" and st["valuations"].requested == 4  # 来自 base
    assert st["indicators"].status == "ok" and st["indicators"].requested == 4  # 本次补取
    assert st["constituents"].status == "ok"
    assert set(merged.metrics_available) == {"pe_ttm", "revenue_yoy"}
    assert merged.universe == base.universe
    # 只为指定 members 补齐
    partial = await repo.build_snapshot(s, faults=set(), metrics={"pe_ttm", "revenue_yoy", "roe"}, base=merged, members=["600001.SH"])
    assert "roe" in partial.stocks["600001.SH"].metrics
    assert "roe" not in partial.stocks["600002.SH"].metrics
    assert ds_map(partial)["indicators"].requested == 5  # 累计请求
    assert "增量补齐" in (ds_map(partial)["indicators"].note or "")


async def test_incremental_base_adds_reference_when_needed() -> None:
    repo, _, _ = make_repo()
    plain = strategy(Condition(id="c1", metric="pe_ttm", op="gt", value=0))
    base = await repo.build_snapshot(plain, faults=set())
    assert base.reference == {}
    ranked = strategy(Condition(id="c2", metric="pe_ttm", op="pct_rank_lte", value=50, rank_scope="industry"))
    snap = await repo.build_snapshot(ranked, faults=set(), base=base)
    assert "pe_ttm" in snap.reference and snap.reference_industry


async def test_report_period_mismatch_with_base_raises() -> None:
    repo, _, _ = make_repo()
    base = await repo.build_snapshot(strategy(*BASIC), faults=set(), metrics={"pe_ttm"})
    with pytest.raises(ValueError):
        await repo.build_snapshot(strategy(*BASIC, report="2025-4"), faults=set(), base=base)


async def test_industry_map_failure_leaves_industry_empty() -> None:
    repo, _, _ = make_repo()
    conds = (Condition(id="c1", metric="gross_margin", op="gte", value=30),)
    snap = await repo.build_snapshot(strategy(*conds), faults={"industry_map"})
    st = ds_map(snap)["industry_map"]
    assert st.status == "failed"
    assert all(sf.info.industry is None for sf in snap.stocks.values())
    bank_gm = snap.stocks["600002.SH"].metrics["gross_margin"]
    assert bank_gm.status == "not_applicable" and "简称" in (bank_gm.reason or "")  # 行业未知时按简称兜底
    assert snap.stocks["600001.SH"].metrics["gross_margin"].status == "ok"


async def test_industry_map_is_cached_daily_but_faults_bypass_memo() -> None:
    repo, client, _ = make_repo()
    first = await repo.industry_map()
    n = client.count("industry_map")
    second = await repo.industry_map()
    assert first == second and client.count("industry_map") == n  # 内存日缓存
    assert first["600001.SH"] == ("881110.TI", "测试电子")
    with pytest.raises(FuyaoError):
        await repo.industry_map(faults={"industry_map"})


async def test_constituents_failure_raises() -> None:
    market = SyntheticMarket()
    market.fail_rules.append(fail_when("constituents", "000300.SH", code=5002, kind="upstream_timeout"))
    repo, _, _ = make_repo(market)
    with pytest.raises(FuyaoError) as ei:
        await repo.build_snapshot(strategy(*BASIC), faults=set())
    assert ei.value.code == 5002


async def test_all_metric_datasets_with_benchmark_and_dividends() -> None:
    conds = tuple(
        Condition(id=f"c{i}", metric=m, op="gt", value=0)
        for i, m in enumerate(["beta_250d", "dividend_yield_ttm", "market_cap_est", "revenue_amount", "pct_change_today"])
    )
    repo, client, _ = make_repo()
    snap = await repo.build_snapshot(strategy(*conds), faults=set())
    st = ds_map(snap)
    for name in ("benchmark", "dividends", "income", "snapshot", "history", "valuations"):
        assert st[name].status == "ok", name
    assert client.count("benchmark") == 1
    s1 = snap.stocks["600001.SH"].metrics
    assert s1["beta_250d"].status == "ok"
    assert s1["dividend_yield_ttm"].value == pytest.approx(0.5 / 20.0 * 100)
    assert s1["market_cap_est"].value == pytest.approx(15.5 * 23.0)  # PE × 归母净利润TTM
    assert s1["revenue_amount"].value == pytest.approx(60.0)
    div_params = [p for _, p, ds in client.calls if ds == "dividends"]
    assert all(p["from"] == "2025-09-12" for p in div_params)  # 今天 − 400 天
    inc_params = [p for _, p, ds in client.calls if ds == "income"]
    assert all(p["period"] == "quarterly" and p["limit"] == 20 for p in inc_params)


async def test_suspended_stock_is_stale_and_trade_date_is_mode() -> None:
    market = SyntheticMarket()
    market.history_end["600002.SH"] = date(2026, 9, 30)  # 模拟停牌
    market.bench_days = market.days + [TODAY]  # 基准多出一根（如盘中K线），不应把全部股票判为过期
    conds = (Condition(id="c1", metric="return_20d", op="gt", value=-100), Condition(id="c2", metric="beta_250d", op="lte", value=5))
    repo, _, _ = make_repo(market)
    snap = await repo.build_snapshot(strategy(*conds), faults=set())
    assert snap.trade_date == LAST_TRADE_DAY.isoformat()
    suspended = snap.stocks["600002.SH"].metrics["return_20d"]
    assert suspended.status == "ok" and suspended.stale is True
    assert "可能停牌" in (suspended.evidence.note or "")
    for code in ("600001.SH", "000003.SZ", "300004.SZ"):
        assert snap.stocks[code].metrics["return_20d"].stale is False
        assert snap.stocks[code].metrics["beta_250d"].status == "ok"


SUSPENDED = "600002.SH"
SUSPENDED_NOTE = "该股最近交易日为 2026-09-30，早于市场最近交易日 2026-10-16（可能停牌），估值为停牌前数值"


def suspended_market() -> SyntheticMarket:
    market = SyntheticMarket()
    market.history_end[SUSPENDED] = date(2026, 9, 30)  # 模拟停牌：日K停在 9-30，快照/估值仍是停牌前数值
    return market


async def test_suspended_stock_valuations_are_stale_and_never_pass() -> None:
    conds = (
        Condition(id="c1", metric="pe_ttm", op="lt", value=30),
        Condition(id="c2", metric="market_cap_est", op="gt", value=0),
        Condition(id="c3", metric="dividend_yield_ttm", op="gte", value=0),
        Condition(id="c4", metric="return_20d", op="gt", value=-100),
    )
    repo, _, _ = make_repo(suspended_market())
    snap = await repo.build_snapshot(strategy(*conds), faults=set())
    m = snap.stocks[SUSPENDED].metrics
    for mid in ("pe_ttm", "pb_mrq", "market_cap_est"):
        assert m[mid].status == "ok" and m[mid].stale is True and m[mid].reason == SUSPENDED_NOTE, mid
    assert m["pe_ttm"].evidence.as_of.date() == date(2026, 9, 30)
    assert m["dividend_yield_ttm"].stale is True and "行情快照为停牌前数值" in (m["dividend_yield_ttm"].reason or "")
    for code in ("600001.SH", "000003.SZ", "300004.SZ"):
        assert not any(snap.stocks[code].metrics[mid].stale for mid in ("pe_ttm", "market_cap_est", "dividend_yield_ttm")), code
    out = evaluate(strategy(*conds), snap)
    bad = next(sr for sr in out.missing if sr.thscode == SUSPENDED)
    by_id = {e.condition_id: e for e in bad.evaluations}
    for cid in ("c1", "c2", "c3", "c4"):
        assert by_id[cid].status == "missing" and "可能停牌" in (by_id[cid].reason or ""), cid
    assert by_id["c1"].reason == f"数据时点过期（{SUSPENDED_NOTE}），不参与判定"
    assert SUSPENDED not in {sr.thscode for sr in out.included}


async def test_suspension_marked_consistently_across_incremental_phases() -> None:
    repo, _, _ = make_repo(suspended_market())
    pe_only = strategy(Condition(id="c1", metric="pe_ttm", op="lt", value=30))
    # 先只取估值（未取日K → 不判断），再补价格窗口指标（全A预筛后深度取数 / what-if 的路径）
    base = await repo.build_snapshot(pe_only, faults=set(), metrics={"pe_ttm"})
    assert base.stocks[SUSPENDED].metrics["pe_ttm"].stale is False
    merged = await repo.build_snapshot(pe_only, faults=set(), metrics={"pe_ttm", "return_20d"}, base=base)
    pe = merged.stocks[SUSPENDED].metrics["pe_ttm"]
    assert pe.stale is True and pe.reason == SUSPENDED_NOTE and pe.value == pytest.approx(5.2)
    assert pe.evidence.period.startswith("该股最近交易日 2026-09-30 收盘（停牌前数值")
    assert base.stocks[SUSPENDED].metrics["pe_ttm"].stale is False  # 不修改基准快照
    assert merged.stocks["600001.SH"].metrics["pe_ttm"].stale is False
    # 反过来：基准快照已取日K，本次只补估值/快照类指标（不再取日K）→ 用同一交易日的基准快照推断
    later = await repo.build_snapshot(pe_only, faults=set(), metrics={"pe_ttm", "return_20d", "dividend_yield_ttm", "ps_ttm"}, base=merged)
    for mid in ("dividend_yield_ttm", "ps_ttm"):
        assert later.stocks[SUSPENDED].metrics[mid].stale is True, mid
        assert later.stocks["600001.SH"].metrics[mid].stale is False, mid
    # 基准快照来自更早的交易日时不推断（避免把旧数据误判为停牌）
    old = merged.model_copy(update={"trade_date": "2026-10-15"})
    other_day = await repo.build_snapshot(pe_only, faults=set(), metrics={"pe_ttm", "return_20d", "ps_ttm"}, base=old)
    assert other_day.stocks[SUSPENDED].metrics["ps_ttm"].stale is False


async def test_all_a_two_phase_run_marks_suspension_and_explains_skips() -> None:
    from app.schemas import ScreenRequest
    from app.services.runs import RunManager

    market = SyntheticMarket()
    market.history_end["600001.SH"] = date(2026, 9, 30)
    repo, _, _ = make_repo(market)
    mgr = RunManager(make_settings(), repo)
    s = Strategy(universe=Universe(kind="all_a"), conditions=[
        Condition(metric="is_st", op="is_false"),
        Condition(metric="pe_ttm", op="between", value=0, value2=30),
        Condition(metric="volatility_250d", op="lt", value=80),
    ])
    res = await mgr.wait(mgr.start(ScreenRequest(strategy=s)), 30)
    assert res.status == "done", res.error
    ev = {sr.thscode: {e.condition_id: e for e in sr.evaluations} for sr in [*res.included, *res.excluded, *res.missing]}
    pe_label = next(c.label for c in res.strategy.conditions if c.metric == "pe_ttm")
    # 预筛阶段的估值在深度取数拿到日K后按停牌补标过期：不能以停牌前估值判为通过
    assert ev["600001.SH"]["c2"].status == "missing" and "估值为停牌前数值" in (ev["600001.SH"]["c2"].reason or "")
    assert "600001.SH" not in {sr.thscode for sr in res.included}
    # 被前置条件排除 vs 前置条件数据缺失
    assert ev["300004.SZ"]["c3"].status == "skipped"
    assert ev["300004.SZ"]["c3"].reason == f"已被前置条件排除（未通过：「{pe_label}」），未取数"
    assert ev["000008.SZ"]["c3"].reason == f"前置条件数据缺失（「{pe_label}」），未进入深度评估"
    deep = next(st for st in res.condition_stats if st.condition_id == "c3")
    assert deep.skipped > 0 and "未评估" in (deep.note or "")


async def test_no_dividend_events_counts_as_success_and_zero_yield() -> None:
    market = SyntheticMarket()

    def no_events(path: str, params: dict[str, Any], ds: str) -> FuyaoError | None:
        if ds == "dividends" and params.get("thscode") == "000003.SZ":
            return FuyaoError("No adjustment events for thscode=000003.SZ", kind="not_ready", code=3002,
                              request_id="rid-nodiv", endpoint=path, dataset=ds, params=params)
        return None

    market.fail_rules.append(no_events)
    market.fail_rules.append(fail_when("dividends", "300004.SZ", code=3002, kind="not_ready", request_id="rid-notready"))
    repo, _, _ = make_repo(market)
    s = strategy(Condition(id="c1", metric="dividend_yield_ttm", op="gte", value=3))
    snap = await repo.build_snapshot(s, faults=set())
    st = ds_map(snap)["dividends"]
    # “无分红事件”计为成功；其他 3002 仍是失败
    assert st.status == "partial" and (st.requested, st.succeeded, st.failed) == (4, 3, 1)
    assert st.errors == ["300004.SZ: code=3002 数据未就绪 (request_id=rid-notready)"]
    assert "1 只数据源无分红事件记录" in (st.note or "")
    mv = snap.stocks["000003.SZ"].metrics["dividend_yield_ttm"]
    assert mv.status == "ok" and mv.value == 0.0
    assert "数据源无分红事件记录（code=3002，request_id=rid-nodiv），按 0 处理" in (mv.evidence.note or "")
    assert snap.stocks["300004.SZ"].metrics["dividend_yield_ttm"].status == "missing"
    out = evaluate(s, snap)
    assert "000003.SZ" in {sr.thscode for sr in out.excluded}  # 股息率 0% < 3%：不满足，而不是“数据缺失”
    assert {sr.thscode for sr in out.missing} == {"300004.SZ"}
    # 只有“无分红事件”时数据集为 ok
    market.fail_rules.pop()
    clean = ds_map(await repo.build_snapshot(s, faults=set()))["dividends"]
    assert clean.status == "ok" and clean.failed == 0 and clean.errors == []


async def test_industry_map_failure_makes_industry_rank_missing() -> None:
    repo, _, _ = make_repo()
    s = strategy(
        Condition(id="c1", metric="pe_ttm", op="pct_rank_lte", value=50, rank_scope="industry"),
        Condition(id="c2", metric="pb_mrq", op="pct_rank_lte", value=50, rank_scope="market"),
    )
    snap = await repo.build_snapshot(s, faults={"industry_map"})
    assert ds_map(snap)["industry_map"].status == "failed"
    out = evaluate(s, snap)
    for sr in [*out.included, *out.excluded, *out.missing]:
        by_id = {e.condition_id: e for e in sr.evaluations}
        c1 = by_id["c1"]
        if c1.value is not None and c1.value > 0:
            assert c1.status == "missing", sr.thscode
            assert (c1.reason or "").startswith("行业归属数据获取失败：") and "code=5003" in (c1.reason or ""), sr.thscode
            assert "演示故障注入" in (c1.reason or "")
        assert by_id["c2"].status in ("pass", "fail", "not_applicable")  # 全A分位不依赖行业归属
    st = next(x for x in out.condition_stats if x.condition_id == "c1")
    assert (st.missing, st.not_applicable) == (3, 1)  # 只有负 PE 的 000003.SZ 是“不适用”


async def test_benchmark_fault_only_breaks_beta() -> None:
    conds = (Condition(id="c1", metric="beta_250d", op="lte", value=1), Condition(id="c2", metric="volatility_250d", op="lte", value=40))
    repo, _, _ = make_repo()
    snap = await repo.build_snapshot(strategy(*conds), faults={"benchmark"})
    st = ds_map(snap)
    assert st["benchmark"].status == "failed" and st["benchmark"].requested == 1
    for sf in snap.stocks.values():
        assert sf.metrics["beta_250d"].status == "error"
        assert sf.metrics["volatility_250d"].status == "ok"


# ───────────────────────────── 股票池 / 搜索 / 行业规则 ─────────────────────────────


async def test_all_a_universe_excludes_delisting_names() -> None:
    repo, _, _ = make_repo()
    u = await repo.resolve_universe(Universe(kind="all_a", code=None, name=None))
    assert u.kind == "all_a"
    assert "600005.SH" not in u.members
    assert u.size == 11 and u.members == sorted(u.members)
    assert "剔除" in (u.note or "")


async def test_custom_universe_validates_codes() -> None:
    repo, _, _ = make_repo()
    u = await repo.resolve_universe(Universe(kind="custom", code=None, name=None, codes=["600001.sh", " 000007.SZ", "bad", "999999.SH", "600001.SH"]))
    assert u.members == ["000007.SZ", "600001.SH"]
    assert "bad" in (u.note or "") and "999999.SH" in (u.note or "")


async def test_resolve_by_query_uses_search() -> None:
    repo, _, _ = make_repo()
    u = await repo.resolve_universe(Universe(kind="industry", code=None, name=None, query="半导体"))
    assert u.code == "881111.TI" and u.kind == "industry"
    assert u.members == sorted(["600001.SH", "300004.SZ", "688006.SH"])
    assert "匹配到" in (u.note or "")


async def test_search_universes_groups_and_limit() -> None:
    repo, _, _ = make_repo()
    hs = await repo.search_universes("沪深300")
    assert hs[0].code == "000300.SH" and hs[0].group == "常用指数"
    lex = await repo.search_universes("沪深三百")
    assert lex and lex[0].code == "000300.SH"
    semi = await repo.search_universes("半导体板块")
    assert semi[0].name == "测试半导体" and semi[0].group == "同花顺行业" and semi[0].kind == "industry"
    wine = await repo.search_universes("白酒")
    assert [o.group for o in wine] == ["同花顺概念"] and wine[0].code == "886001.TI"
    alla = await repo.search_universes("全A")
    assert alla[0].kind == "all_a" and alla[0].group == "全市场"
    ceshi = await repo.search_universes("测试")
    assert 0 < len(ceshi) <= 20
    assert {o.group for o in ceshi} == {"同花顺行业", "同花顺概念"}
    assert await repo.search_universes("") == (await repo.search_universes("  "))


def test_pick_industry_rule() -> None:
    assert INDUSTRY_PICK_RULE["min_members"] == 8
    # 有 ≥ 8 的：取其中成员最少（最细）
    assert pick_industry([("A", "一级", 30), ("B", "二级", 8), ("C", "三级", 3)]) == ("B", "二级")
    # 全部 < 8：取成员最多
    assert pick_industry([("C", "三级", 3), ("D", "小类", 5)]) == ("D", "小类")
    # 并列取代码较小者（确定性）
    assert pick_industry([("Y", "乙", 10), ("X", "甲", 10)]) == ("X", "甲")
    assert pick_industry([]) is None
    assert pick_industry([("A", "一级", 30), ("B", "二级", 9)], {"min_members": 10, "prefer": "fewest", "fallback": "most"}) == ("A", "一级")


def test_merge_dataset_status_rules() -> None:
    base = DatasetStatus(dataset="indicators", label="L", endpoint="E", status="ok", requested=4, succeeded=4)
    new = DatasetStatus(dataset="indicators", label="L", endpoint="E", status="failed", requested=1, failed=1, errors=["x"])
    skipped = DatasetStatus(dataset="indicators", label="L", endpoint="E", status="skipped")
    assert merge_dataset_status(base, skipped) is base
    assert merge_dataset_status(skipped, new) is new
    m = merge_dataset_status(base, new)
    assert (m.status, m.requested, m.succeeded, m.failed, m.errors) == ("partial", 5, 4, 1, ["x"])


async def test_raw_fetch_methods_for_services() -> None:
    repo, client, _ = make_repo()
    h = await repo.history("600001.SH", 1, 2)
    assert h.items and h.params["adjust"] == "backward"
    ih = await repo.index_history("000300.SH", 1, 2)
    assert ih.dataset == "benchmark"
    inc = await repo.income_quarterly("600001.SH")
    assert inc.params == {"thscode": "600001.SH", "period": "quarterly", "limit": 20}
    cal = await repo.trading_days()
    assert cal.items
    batch = await repo.valuations(["600001.SH", "bad-code", "000007.SZ"])
    assert set(batch.rows) == {"600001.SH", "000007.SZ"}
    assert batch.status is not None and batch.status.requested == 2


async def test_wiring_with_real_client_and_mock_transport() -> None:
    market = SyntheticMarket()

    def handler(request: httpx.Request) -> httpx.Response:
        env = market.handle(request.url.path, dict(request.url.params))
        assert request.headers["X-api-key"] == "test-key"
        return httpx.Response(200, json=env)

    settings = make_settings()
    client = FuyaoClient(settings, ApiCache(":memory:"), transport=httpx.MockTransport(handler), backoff_base_s=0)
    repo = Repository(client, settings, today=lambda: TODAY)
    snap = await repo.build_snapshot(strategy(*BASIC), faults={"history"})
    await client.aclose()
    st = ds_map(snap)
    assert st["indicators"].status == "ok" and st["history"].status == "failed"
    assert snap.stocks["600001.SH"].metrics["roe_change"].value == pytest.approx(1.0)
    assert snap.stocks["600001.SH"].metrics["volatility_250d"].status == "error"
    assert snap.trade_date == "2026-10-16"  # 无日K时回退到交易日历的最近交易日
