"""engine/evaluate.py：判定全路径、缺失策略、软分排序、分位、漏斗、统计、差一点入选、覆盖率、确定性、预筛、性能。"""

from __future__ import annotations

import time

import pytest

from app.engine.conditions import strategy_hash
from app.engine.evaluate import FUNNEL_NOTE, InvalidStrategyError, evaluate, prefilter
from app.schemas import DatasetStatus, EvaluationOutput, StockResult
from tests._fixtures_engine import error, make_snapshot, make_stock, make_strategy, missing, not_applicable, ok
from tests._fixtures_engine import make_condition as C
from tests._fixtures_engine import synthetic_snapshot


def all_results(out: EvaluationOutput) -> list[StockResult]:
    return [*out.included, *out.excluded, *out.missing]


def evals(out: EvaluationOutput, cond_id: str = "c1") -> dict:
    return {sr.thscode: next(e for e in sr.evaluations if e.condition_id == cond_id) for sr in all_results(out)}


def codes(results: list[StockResult]) -> list[str]:
    return [s.thscode for s in results]


def stat(out: EvaluationOutput, cond_id: str):
    return next(s for s in out.condition_stats if s.condition_id == cond_id)


# ───────────────────────────── 判定路径 ─────────────────────────────


def test_every_status_path_with_reason():
    snap = make_snapshot([
        make_stock("600001.SH", {"revenue_yoy": 12.0}),
        make_stock("600002.SH", {"revenue_yoy": -3.0}),
        make_stock("600003.SH", {"revenue_yoy": missing("revenue_yoy")}),
        make_stock("600004.SH", {"revenue_yoy": error("revenue_yoy", "code=5003 上游服务不可用 request_id=req-1")}),
        make_stock("600005.SH", {"revenue_yoy": not_applicable("revenue_yoy", "该期不适用（测试）")}),
        make_stock("600006.SH", {}),
        make_stock("600007.SH", {"revenue_yoy": ok("revenue_yoy", 8.0, stale=True)}),
        make_stock("600008.SH", {"revenue_yoy": float("nan")}),
        make_stock("600009.SH", {"revenue_yoy": True}),
    ])
    out = evaluate(make_strategy(C("revenue_yoy", "gt", 0)), snap)
    ev = evals(out)
    assert ev["600001.SH"].status == "pass" and ev["600001.SH"].margin == 12.0  # 阈值为 0 时 margin 用绝对差
    assert ev["600001.SH"].threshold == "> 0%" and ev["600001.SH"].display == "12.00%"
    assert ev["600001.SH"].evidence is not None and ev["600001.SH"].value == 12.0
    assert ev["600002.SH"].status == "fail" and ev["600002.SH"].margin == -3.0
    assert ev["600003.SH"].status == "missing" and ev["600003.SH"].reason == "接口返回空值（该期未披露或不适用）"
    assert ev["600004.SH"].status == "missing"
    assert ev["600004.SH"].reason.startswith("数据获取失败：") and "req-1" in ev["600004.SH"].reason
    assert ev["600005.SH"].status == "not_applicable" and ev["600005.SH"].reason == "该期不适用（测试）"
    assert ev["600006.SH"].status == "missing" and ev["600006.SH"].reason == "未获取该指标"
    assert ev["600007.SH"].status == "missing" and "过期" in ev["600007.SH"].reason
    assert ev["600007.SH"].value == 8.0  # 过期值仍展示，但不判为通过
    assert ev["600008.SH"].status == "missing" and "非有限数" in ev["600008.SH"].reason
    assert ev["600009.SH"].status == "missing" and "类型不符" in ev["600009.SH"].reason
    for st in ("missing", "not_applicable"):
        assert all(e.margin is None for e in ev.values() if e.status == st)
    assert codes(out.included) == ["600001.SH"]
    assert codes(out.excluded) == ["600002.SH"]
    assert codes(out.missing) == [f"60000{i}.SH" for i in range(3, 10)]
    assert out.excluded[0].failed_conditions == ["c1"]
    assert out.missing[0].missing_conditions == ["c1"]


def test_stale_reason_falls_back_to_evidence_note():
    mv = ok("volatility_250d", 22.0, stale=True)
    mv.evidence.note = "该股最近交易日早于市场最近交易日（可能停牌）"
    snap = make_snapshot([make_stock("600001.SH", {"volatility_250d": mv})])
    out = evaluate(make_strategy(C("volatility_250d", "lte", 30)), snap)
    e = evals(out)["600001.SH"]
    assert e.status == "missing" and "可能停牌" in e.reason and e.display == "22.00%"
    assert codes(out.missing) == ["600001.SH"]


def test_stale_valuation_never_passes_and_reason_carries_suspension_note():
    note = "该股最近交易日为 2026-09-14，早于市场最近交易日 2026-10-16（可能停牌），估值为停牌前数值"
    pe = ok("pe_ttm", 25.66, stale=True, reason=note)
    cap = ok("market_cap_est", 504.6, stale=True, reason=note)
    snap = make_snapshot([
        make_stock("601059.SH", {"pe_ttm": pe, "market_cap_est": cap}),
        make_stock("600001.SH", {"pe_ttm": 12.0, "market_cap_est": 800.0}),
    ])
    for policy in ("exclude", "keep_flagged"):
        out = evaluate(make_strategy(C("pe_ttm", "lt", 30), C("market_cap_est", "gt", 0), policy=policy), snap)
        for cid in ("c1", "c2"):
            e = evals(out, cid)["601059.SH"]
            assert e.status == "missing" and e.reason == f"数据时点过期（{note}），不参与判定", (policy, cid)
            assert e.margin is None
        assert evals(out, "c1")["600001.SH"].status == "pass"
    assert codes(evaluate(make_strategy(C("pe_ttm", "lt", 30)), snap).missing) == ["601059.SH"]


def test_bool_condition():
    snap = make_snapshot([make_stock("600001.SH", {"is_st": False}), make_stock("600002.SH", {"is_st": True})])
    out = evaluate(make_strategy(C("is_st", "is_false")), snap)
    ev = evals(out)
    assert ev["600001.SH"].status == "pass" and ev["600002.SH"].status == "fail"
    assert ev["600002.SH"].display == "是" and ev["600002.SH"].threshold == "否" and ev["600002.SH"].margin is None


def test_prefiltered_missing_key_is_skipped_and_never_included():
    snap = make_snapshot([
        make_stock("600001.SH", {"pe_ttm": 10.0, "roe": 9.0}),
        make_stock("600002.SH", {"pe_ttm": 12.0}),  # 预筛后未取 roe
    ], prefiltered=True)
    for policy in ("exclude", "keep_flagged"):
        out = evaluate(make_strategy(C("pe_ttm", "gt", 0), C("roe", "gt", 5), policy=policy), snap)
        ev = evals(out, "c2")
        assert ev["600002.SH"].status == "skipped" and ev["600002.SH"].reason == "已被前置条件排除，未取数"
        assert codes(out.included) == ["600001.SH"]
        assert codes(out.missing) == ["600002.SH"]
        assert stat(out, "c2").skipped == 1


def test_invalid_enabled_condition_raises_but_disabled_is_ignored():
    snap = make_snapshot([make_stock("600001.SH", {"pe_ttm": 10.0})])
    with pytest.raises(InvalidStrategyError) as exc:
        evaluate(make_strategy(C("pe_ttm", "gt", 0), C("is_st", "gt", 1)), snap)
    assert "不支持算子" in str(exc.value)
    out = evaluate(make_strategy(C("pe_ttm", "gt", 0), C("is_st", "gt", 1, enabled=False)), snap)
    assert codes(out.included) == ["600001.SH"]
    assert [e.condition_id for e in out.included[0].evaluations] == ["c1"]  # 停用条件不参与判定


# ───────────────────────────── 缺失策略 ─────────────────────────────


def _policy_snapshot():
    return make_snapshot([
        make_stock("600001.SH", {"pe_ttm": 10.0, "gross_margin": 45.0}),
        make_stock("600002.SH", {"pe_ttm": -2.0, "gross_margin": 50.0}),
        make_stock("600003.SH", {"pe_ttm": 15.0, "gross_margin": None}),
        make_stock("600004.SH", {"pe_ttm": 18.0, "gross_margin": not_applicable("gross_margin", "银行无毛利率（测试）")}),
        make_stock("600005.SH", {"pe_ttm": -1.0, "gross_margin": None}),
    ])


def test_missing_policy_exclude():
    out = evaluate(make_strategy(C("pe_ttm", "gt", 0), C("gross_margin", "gte", 30)), _policy_snapshot())
    assert codes(out.included) == ["600001.SH"]
    assert codes(out.excluded) == ["600002.SH", "600005.SH"]  # 有 fail 即排除，即使另一条缺失
    assert codes(out.missing) == ["600003.SH", "600004.SH"]
    assert out.missing[0].missing_conditions == ["c2"] and not out.missing[0].flagged
    assert out.excluded[1].missing_conditions == ["c2"] and out.excluded[1].failed_conditions == ["c1"]


def test_missing_policy_keep_flagged():
    out = evaluate(make_strategy(C("pe_ttm", "gt", 0), C("gross_margin", "gte", 30), policy="keep_flagged"),
                   _policy_snapshot())
    assert codes(out.included) == ["600001.SH", "600003.SH", "600004.SH"]
    assert [s.flagged for s in out.included] == [False, True, True]
    assert codes(out.excluded) == ["600002.SH", "600005.SH"]
    assert out.missing == []


# ───────────────────────────── 软条件与排序 ─────────────────────────────


def test_soft_score_and_sorting():
    snap = make_snapshot([
        make_stock("600001.SH", {"pe_ttm": 10.0, "roe": 8.0, "debt_ratio": 40.0}),
        make_stock("600002.SH", {"pe_ttm": 12.0, "roe": 9.0, "debt_ratio": 60.0}),
        make_stock("600003.SH", {"pe_ttm": 15.0, "roe": 3.0, "debt_ratio": 30.0}),
        make_stock("600004.SH", {"pe_ttm": 20.0, "roe": None, "debt_ratio": 45.0}),
        make_stock("600005.SH", {"pe_ttm": 25.0, "roe": 4.0, "debt_ratio": 70.0}),
        make_stock("600006.SH", {"pe_ttm": -5.0, "roe": 20.0, "debt_ratio": 10.0}),
    ])
    s = make_strategy(C("pe_ttm", "gt", 0), C("roe", "gt", 5, hard=False, weight=2), C("debt_ratio", "lte", 50, hard=False),
                      sort_metric="roe")
    out = evaluate(s, snap)
    assert codes(out.included) == ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"]
    assert [x.soft_score for x in out.included] == [1.0, 0.666667, 0.333333, 0.333333, 0.0]
    # 软条件缺失按未满足计 0 分；排序指标缺失的排在同分股票之后
    assert out.excluded[0].soft_score == 1.0 and out.excluded[0].bucket == "excluded"
    assert out.included[0].failed_conditions == [] and out.included[1].failed_conditions == []
    st = stat(out, "c2")
    assert st.hard is False and st.drop_effect == 5 and st.sweep == [] and "软条件" in st.note


def test_sort_metric_direction_and_code_tiebreak():
    snap = make_snapshot([
        make_stock("600003.SH", {"pe_ttm": 30.0}),
        make_stock("600001.SH", {"pe_ttm": 20.0}),
        make_stock("600002.SH", {"pe_ttm": 20.0}),
        make_stock("600004.SH", {"pe_ttm": None}),
    ])
    asc = evaluate(make_strategy(C("pe_ttm", "gt", 0), sort_metric="pe_ttm", sort_desc=False, policy="keep_flagged"), snap)
    assert codes(asc.included) == ["600001.SH", "600002.SH", "600003.SH", "600004.SH"]
    desc = evaluate(make_strategy(C("pe_ttm", "gt", 0), sort_metric="pe_ttm", policy="keep_flagged"), snap)
    assert codes(desc.included) == ["600003.SH", "600001.SH", "600002.SH", "600004.SH"]
    no_sort = evaluate(make_strategy(C("pe_ttm", "gt", 0)), snap)
    assert codes(no_sort.included) == ["600001.SH", "600002.SH", "600003.SH"]


# ───────────────────────────── 分位 ─────────────────────────────


def test_rank_average_ties_universe():
    vols = {"600001.SH": 10.0, "600002.SH": 20.0, "600003.SH": 20.0, "600004.SH": 30.0, "600005.SH": 40.0}
    snap = make_snapshot([make_stock(c, {"volatility_250d": v}) for c, v in vols.items()])
    out = evaluate(make_strategy(C("volatility_250d", "pct_rank_lte", 40, scope="universe")), snap)
    ev = evals(out)
    assert {c: e.rank_pct for c, e in ev.items()} == {
        "600001.SH": 0.0, "600002.SH": 37.5, "600003.SH": 37.5, "600004.SH": 75.0, "600005.SH": 100.0}
    assert all(e.rank_group_size == 5 for e in ev.values())
    assert [ev[c].status for c in vols] == ["pass", "pass", "pass", "fail", "fail"]
    assert ev["600002.SH"].margin == 0.025 and ev["600004.SH"].margin == -0.35
    assert ev["600001.SH"].threshold == "股票池内分位 ≤ 40%"


def test_rank_positive_only_filters_non_positive_values():
    pes = {"600001.SH": -5.0, "600002.SH": 0.0, "600003.SH": 10.0, "600004.SH": 20.0,
           "600005.SH": 30.0, "600006.SH": 40.0, "600007.SH": 50.0}
    snap = make_snapshot([make_stock(c, {"pe_ttm": v}) for c, v in pes.items()])
    out = evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="universe")), snap)
    ev = evals(out)
    assert ev["600001.SH"].status == "not_applicable" and ev["600001.SH"].reason == "PE 为负（亏损），分位不适用"
    assert ev["600002.SH"].status == "not_applicable" and ev["600002.SH"].reason == "PE 为 0，分位不适用"
    assert ev["600001.SH"].value == -5.0 and ev["600001.SH"].rank_pct is None
    assert [ev[c].rank_pct for c in list(pes)[2:]] == [0.0, 25.0, 50.0, 75.0, 100.0]
    assert codes(out.included) == ["600003.SH", "600004.SH", "600005.SH"]
    assert codes(out.missing) == ["600001.SH", "600002.SH"]


def _industry_snapshot(with_reference: bool = True):
    ref = {f"R{i}.SZ": float(10 * i) for i in range(1, 7)}  # 行业 A：10..60
    ref.update({f"S{i}.SZ": float(100 * i) for i in range(1, 6)})  # 行业 B：100..500
    ref.update({"Q1.SZ": 5.0, "Q2.SZ": 6.0})  # 行业 C：只有 2 只同行
    ref["600001.SH"] = 26.0  # 参考组里的旧值，应以本股自身取数为准
    ref_ind = {**{f"R{i}.SZ": "A" for i in range(1, 7)}, **{f"S{i}.SZ": "B" for i in range(1, 6)},
               "Q1.SZ": "C", "Q2.SZ": "C", "600001.SH": "A"}
    stocks = [
        make_stock("600001.SH", {"pe_ttm": 25.0}, industry="行业A", industry_code="A"),
        make_stock("600002.SH", {"pe_ttm": 250.0}, industry="行业B", industry_code="B"),
        make_stock("600003.SH", {"pe_ttm": 30.0}),  # 行业未知
        make_stock("600004.SH", {"pe_ttm": 15.0}, industry="行业C", industry_code="C"),
        make_stock("600005.SH", {"pe_ttm": -8.0}, industry="行业A", industry_code="A"),
    ]
    return make_snapshot(stocks, reference={"pe_ttm": ref} if with_reference else {}, reference_industry=ref_ind)


def test_rank_industry_group():
    out = evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="industry")), _industry_snapshot())
    ev = evals(out)
    # 行业 A：[10,20,25,30,40,50,60]，本股 25 排第 3 → 100×2/6
    assert ev["600001.SH"].rank_pct == 33.3333 and ev["600001.SH"].rank_group_size == 7
    assert ev["600001.SH"].status == "pass"
    # 行业 B：[100,200,250,300,400,500] → 100×2/5
    assert ev["600002.SH"].rank_pct == 40.0 and ev["600002.SH"].rank_group_size == 6
    assert ev["600003.SH"].status == "not_applicable" and ev["600003.SH"].reason == "行业归属未知"
    assert ev["600004.SH"].status == "not_applicable" and ev["600004.SH"].reason == "参考组样本不足（n=3）"
    assert ev["600004.SH"].rank_group_size == 3
    assert ev["600005.SH"].status == "not_applicable" and "亏损" in ev["600005.SH"].reason


def _industry_map_status(status: str, *, succeeded: int = 0, requested: int = 1) -> DatasetStatus:
    return DatasetStatus(
        dataset="industry_map", label="同花顺行业归属（行业指数成分反查）", endpoint="/api/a-share-index/catalog/ths-index-list + constituents",
        status=status, requested=requested, succeeded=succeeded, failed=requested - succeeded,  # type: ignore[arg-type]
        errors=["同花顺行业指数目录: code=5003 上游不可用 (request_id=rid-ind) 【演示故障注入】"],
    )


def test_rank_industry_missing_when_industry_map_failed():
    base = _industry_snapshot()
    no_industry = [make_stock(c, {"pe_ttm": v}) for c, v in (("600001.SH", 25.0), ("600002.SH", 250.0), ("600003.SH", 30.0))]
    snap = make_snapshot(no_industry, reference=base.reference, reference_industry={}, datasets=[_industry_map_status("failed")])
    out = evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="industry")), snap)
    for e in evals(out).values():
        assert e.status == "missing"  # 接口失败造成的“无行业”是数据缺失，不是“不适用”
        assert e.reason.startswith("行业归属数据获取失败：") and "code=5003" in e.reason and "rid-ind" in e.reason
    st = stat(out, "c1")
    assert (st.missing, st.not_applicable) == (3, 0)
    assert codes(out.missing) == ["600001.SH", "600002.SH", "600003.SH"]
    # 部分失败：未取得行业的股票同样按缺失处理，并说明是部分失败
    partial = make_snapshot(_industry_snapshot().stocks, reference=base.reference, reference_industry=base.reference_industry,
                            datasets=[_industry_map_status("partial", succeeded=80, requested=91)])
    ev = evals(evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="industry")), partial))
    assert ev["600003.SH"].status == "missing"
    assert ev["600003.SH"].reason.startswith("行业归属数据获取失败：部分失败（成功 80/91，错误样例 ") and "rid-ind" in ev["600003.SH"].reason
    assert ev["600001.SH"].status == "pass"  # 已取得行业的股票照常判定
    # 全A分位不依赖行业归属；行业映射正常时行业未知仍为“不适用”
    market = evals(evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="market")), snap))
    assert all(e.status in ("pass", "fail") for e in market.values())
    ok_map = evals(evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="industry")),
                            make_snapshot(no_industry, reference=base.reference, reference_industry={}, datasets=[_industry_map_status("ok", succeeded=1)])))
    assert all(e.status == "not_applicable" and e.reason == "行业归属未知" for e in ok_map.values())


def test_rank_market_group_and_missing_reference():
    out = evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="market")), _industry_snapshot())
    ev = evals(out)
    # 参考组 14 个值（含被本股覆盖的 600001.SH）+ 不在参考组中的 3 只本股（250、30、15）→ 17 个正值
    assert ev["600001.SH"].rank_group_size == 17
    assert ev["600003.SH"].status in ("pass", "fail")  # 全A分位不需要行业归属
    no_ref = evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 50, scope="industry")), _industry_snapshot(False))
    ev2 = evals(no_ref)
    assert ev2["600001.SH"].status == "missing" and ev2["600001.SH"].reason == "参考组数据不可用"
    assert ev2["600005.SH"].status == "not_applicable"  # 负 PE 先按“不适用”处理


def test_universe_rank_group_too_small():
    snap = make_snapshot([make_stock(f"60000{i}.SH", {"roe": float(i)}) for i in range(1, 4)])
    ev = evals(evaluate(make_strategy(C("roe", "pct_rank_gte", 50, scope="universe")), snap))
    assert all(e.status == "not_applicable" and e.reason == "参考组样本不足（n=3）" for e in ev.values())


# ───────────────────────────── 漏斗与统计 ─────────────────────────────


def _funnel_snapshot():
    rows = {
        "600001.SH": (False, 1000, 5.0),
        "600002.SH": (True, 1000, 5.0),
        "600003.SH": (False, 100, 5.0),
        "600004.SH": (False, 1000, -1.0),
        "600005.SH": (False, 1000, None),
        "600006.SH": (True, 100, -1.0),
    }
    return make_snapshot([make_stock(c, {"is_st": st, "listing_days": d, "revenue_yoy": r}) for c, (st, d, r) in rows.items()],
                         universe_name="测试池A")


FUNNEL_STRATEGY = (C("is_st", "is_false"), C("listing_days", "gte", 365), C("revenue_yoy", "gt", 0))


def test_funnel_steps_exclude_policy():
    out = evaluate(make_strategy(*FUNNEL_STRATEGY), _funnel_snapshot())
    f = out.funnel
    assert f[0].condition_id is None and f[0].label == "股票池：测试池A" and f[0].remaining == 6
    assert [(s.condition_id, s.remaining, s.removed_fail, s.removed_missing) for s in f[1:]] == [
        ("c1", 4, 2, 0), ("c2", 3, 1, 0), ("c3", 1, 1, 1)]
    assert f[1].label == "是否ST：否"
    assert f[-1].remaining == len(out.included) == 1
    assert "顺序不影响最终入选结果" in FUNNEL_NOTE


def test_funnel_keep_flagged_does_not_remove_missing():
    out = evaluate(make_strategy(*FUNNEL_STRATEGY, policy="keep_flagged"), _funnel_snapshot())
    assert [(s.remaining, s.removed_missing) for s in out.funnel[1:]] == [(4, 0), (3, 0), (2, 0)]
    assert out.funnel[-1].remaining == len(out.included) == 2


def test_condition_counts_sole_blocker_and_drop_effect():
    snap = _funnel_snapshot()
    s = make_strategy(*FUNNEL_STRATEGY)
    out = evaluate(s, snap)
    c1, c2, c3 = stat(out, "c1"), stat(out, "c2"), stat(out, "c3")
    assert (c1.passed, c1.failed, c1.missing) == (4, 2, 0)
    assert (c3.passed, c3.failed, c3.missing) == (3, 2, 1)
    assert (c1.sole_blocker, c2.sole_blocker, c3.sole_blocker) == (1, 1, 1)  # 只因缺失进缺失组的不算单独卡点
    assert (c1.drop_effect, c2.drop_effect, c3.drop_effect) == (2, 2, 3)
    # 与“去掉条件后重新评估”的结果一致
    for i, st in enumerate((c1, c2, c3)):
        rest = [c for j, c in enumerate(FUNNEL_STRATEGY) if j != i]
        assert len(evaluate(make_strategy(*rest), snap).included) == st.drop_effect


def test_sweep_points_on_condition_stat():
    snap = synthetic_snapshot(200, seed=5, metrics=["pe_ttm", "roe", "is_st"])
    out = evaluate(make_strategy(C("pe_ttm", "lte", 25), C("is_st", "is_false")), snap)
    sw = stat(out, "c1").sweep
    assert [p.value for p in sw] == [12.5, 15.0, 18.75, 20.0, 25.0, 30.0, 31.25, 37.5, 40.0]
    assert [p.value for p in sw if p.is_current] == [25.0]
    assert next(p.included for p in sw if p.is_current) == len(out.included)
    counts = [p.included for p in sw]
    assert counts == sorted(counts)  # 放宽上限，入选数单调不减
    for p in sw:
        brute = evaluate(make_strategy(C("pe_ttm", "lte", p.value), C("is_st", "is_false")), snap, compute_stats=False)
        assert len(brute.included) == p.included
    assert stat(out, "c2").sweep == []  # 布尔条件无扫描


def test_rank_and_between_sweeps():
    snap = synthetic_snapshot(200, seed=6, metrics=["pe_ttm"])
    out = evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 35, scope="universe"), C("pe_ttm", "between", 0, 40)), snap)
    rank_sw = stat(out, "c1").sweep
    assert [p.value for p in rank_sw] == [20.0, 30.0, 35.0, 40.0, 50.0, 60.0, 70.0, 80.0]
    assert [p.value for p in rank_sw if p.is_current] == [35.0]
    between_sw = stat(out, "c2").sweep
    assert [p.value for p in between_sw if p.is_current] == [40.0]  # between 扫上界
    assert all(p.value >= 0 for p in between_sw) and len(between_sw) <= 9
    for p in between_sw:
        brute = evaluate(make_strategy(C("pe_ttm", "pct_rank_lte", 35, scope="universe"),
                                       C("pe_ttm", "between", 0, p.value)), snap, compute_stats=False)
        assert len(brute.included) == p.included


def test_compute_stats_false_skips_drop_and_sweep():
    out = evaluate(make_strategy(*FUNNEL_STRATEGY), _funnel_snapshot(), compute_stats=False)
    assert all(s.drop_effect is None and s.sweep == [] for s in out.condition_stats)
    assert stat(out, "c1").sole_blocker == 1 and len(out.funnel) == 4


# ───────────────────────────── 差一点入选 ─────────────────────────────


def test_near_miss_numeric():
    snap = make_snapshot([
        make_stock("600001.SH", {"roe": 9.0, "debt_ratio": 50.0}),  # 差 10%，其余满足
        make_stock("600002.SH", {"roe": 5.0, "debt_ratio": 50.0}),  # 差 50%
        make_stock("600003.SH", {"roe": 9.0, "debt_ratio": 70.0}),  # 两条不满足
        make_stock("600004.SH", {"roe": 9.5, "debt_ratio": None}),  # 另一条缺失
        make_stock("600005.SH", {"roe": 12.0, "debt_ratio": 61.0}),  # 债务率差 1.7%
    ])
    s = make_strategy(C("roe", "gt", 10), C("debt_ratio", "lte", 60))
    out = evaluate(s, snap)
    near = {x.thscode: x.near_miss for x in all_results(out)}
    assert near == {"600001.SH": True, "600002.SH": False, "600003.SH": False, "600004.SH": False, "600005.SH": True}
    flagged = evaluate(make_strategy(C("roe", "gt", 10), C("debt_ratio", "lte", 60), policy="keep_flagged"), snap)
    assert {x.thscode: x.near_miss for x in all_results(flagged)}["600004.SH"] is True


def test_near_miss_rank_within_ten_points():
    snap = make_snapshot([make_stock(f"6000{i:02d}.SH", {"volatility_250d": float(i)}) for i in range(11)])
    out = evaluate(make_strategy(C("volatility_250d", "pct_rank_lte", 50, scope="universe")), snap)
    near = {x.thscode: x.near_miss for x in out.excluded}
    assert near["600006.SH"] is True  # 分位 60，差 10 个点
    assert near["600007.SH"] is False  # 分位 70


# ───────────────────────────── 覆盖率 ─────────────────────────────


def test_data_coverage_conflict():
    stocks = [make_stock(f"60000{i}.SH", {"gross_margin": 40.0, "roe": 8.0}) for i in range(1, 5)]
    stocks += [make_stock("600005.SH", {"gross_margin": None, "roe": 8.0}),
               make_stock("600006.SH", {"gross_margin": not_applicable("gross_margin"), "roe": None})]
    out = evaluate(make_strategy(C("gross_margin", "gte", 30), C("roe", "gt", 5)), make_snapshot(stocks))
    assert [c.id for c in out.conflicts] == ["data_coverage:c1"]  # roe 只缺 1/6（17%）
    c = out.conflicts[0]
    assert c.severity == "warning" and c.kind == "data_coverage" and c.condition_ids == ["c1"]
    assert "2/6" in c.message and "33%" in c.message and "主要原因" in c.message and c.suggestion


def test_data_coverage_groups_reasons_ignoring_request_id_code_and_attempts():
    def fail_reason(i: int) -> str:
        attempts = "（共尝试 2 次）" if i % 2 else ""
        return f"财务指标（本期）获取失败：code=5003 上游不可用 (request_id=rid-{i:04d})，Data source unavailable{attempts}"

    stocks = [make_stock(f"6000{i:02d}.SH", {"roe": error("roe", fail_reason(i))}) for i in range(1, 7)]
    stocks += [make_stock(f"0000{i:02d}.SZ", {"roe": missing("roe", f"数据未就绪，No adjustment events for thscode=0000{i:02d}.SZ")})
               for i in range(1, 4)]
    stocks += [make_stock("600099.SH", {"roe": 8.0})]
    out = evaluate(make_strategy(C("roe", "gt", 5)), make_snapshot(stocks))
    msg = out.conflicts[0].message
    assert "9/10" in msg
    # 6 只同一错误码与类别合并计数（逐股 request_id、重试次数不同），样例保留其中一个 request_id
    assert "code=5003 上游不可用" in msg and "（6 只，request_id 为其中一例）" in msg
    assert msg.count("request_id=rid-") == 1
    # 只有代码不同的原因也合并
    only_codes = [make_stock(f"0000{i:02d}.SZ", {"roe": missing("roe", f"No adjustment events for thscode=0000{i:02d}.SZ")}) for i in range(1, 4)]
    msg2 = evaluate(make_strategy(C("roe", "gt", 5)), make_snapshot(only_codes)).conflicts[0].message
    assert "（3 只）" in msg2
    # 无 request_id 的占位“--”（如演示故障注入）不声称“request_id 为其中一例”
    sim = [make_stock(f"60000{i}.SH", {"roe": error("roe", "财务指标（本期）获取失败：code=5003 上游不可用 (request_id=--) 【演示故障注入】")})
           for i in range(1, 4)]
    msg3 = evaluate(make_strategy(C("roe", "gt", 5)), make_snapshot(sim)).conflicts[0].message
    assert "（3 只）" in msg3 and "request_id 为其中一例" not in msg3


# ───────────────────────────── 确定性 ─────────────────────────────


def _mixed_strategy():
    return make_strategy(
        C("is_st", "is_false"), C("listing_days", "gte", 365), C("pe_ttm", "gt", 0),
        C("pe_ttm", "pct_rank_lte", 50, scope="industry"), C("revenue_yoy", "gt", 0),
        C("volatility_250d", "pct_rank_lte", 60, scope="universe"),
        C("roe_annualized", "gte", 8, hard=False, weight=2), C("dividend_yield_ttm", "gte", 2, hard=False),
        sort_metric="roe_annualized",
    )


def test_deterministic_output_regardless_of_input_order():
    snap = synthetic_snapshot(300, seed=3)
    s = _mixed_strategy()
    first = evaluate(s, snap).model_dump()
    assert evaluate(s, snap).model_dump() == first
    shuffled = snap.model_copy(update={"stocks": dict(reversed(list(snap.stocks.items())))})
    assert evaluate(s, shuffled).model_dump() == first
    assert first["strategy_hash"] == strategy_hash(s)


def test_evaluate_does_not_mutate_inputs():
    snap = synthetic_snapshot(80, seed=4)
    s = _mixed_strategy()
    snap_before, s_before = snap.model_dump(), s.model_dump()
    evaluate(s, snap)
    prefilter(s, snap, {"pe_ttm", "is_st", "listing_days"})
    assert snap.model_dump() == snap_before and s.model_dump() == s_before


def test_empty_snapshot():
    out = evaluate(make_strategy(C("pe_ttm", "gt", 0)), make_snapshot([]))
    assert out.included == [] and out.funnel[0].remaining == 0 and out.conflicts == []
    assert stat(out, "c1").drop_effect == 0


# ───────────────────────────── 预筛（all_a 两段式） ─────────────────────────────

CHEAP = {"pe_ttm", "is_st", "listing_days"}


def _prefilter_strategy(policy: str = "exclude"):
    return make_strategy(C("pe_ttm", "gt", 0), C("is_st", "is_false"), C("revenue_yoy", "gt", 0),
                         C("roe", "gt", 5, hard=False), policy=policy)


def _cheap_stocks():
    return {
        "600001.SH": {"pe_ttm": 10.0, "is_st": False},
        "600002.SH": {"pe_ttm": -1.0, "is_st": False},
        "600003.SH": {"pe_ttm": None, "is_st": False},
        "600004.SH": {"pe_ttm": 12.0, "is_st": True},
        "600005.SH": {"pe_ttm": 14.0, "is_st": False},
    }


def test_prefilter_is_policy_aware():
    phase1 = make_snapshot([make_stock(c, m) for c, m in _cheap_stocks().items()])
    assert prefilter(_prefilter_strategy(), phase1, CHEAP) == ["600001.SH", "600005.SH"]
    assert prefilter(_prefilter_strategy("keep_flagged"), phase1, CHEAP) == ["600001.SH", "600003.SH", "600005.SH"]


def test_prefiltered_evaluation_stats_and_funnel():
    deep = {"600001.SH": {"revenue_yoy": 5.0, "roe": 8.0}, "600005.SH": {"revenue_yoy": -2.0, "roe": 3.0}}
    stocks = [make_stock(c, {**m, **deep.get(c, {})}) for c, m in _cheap_stocks().items()]
    snap = make_snapshot(stocks, prefiltered=True)
    # 条件顺序故意把深度条件放前面：漏斗按实际评估顺序（全量取数的条件在前）
    s = make_strategy(C("revenue_yoy", "gt", 0), C("pe_ttm", "gt", 0), C("is_st", "is_false"))
    out = evaluate(s, snap)
    assert codes(out.included) == ["600001.SH"]
    assert codes(out.excluded) == ["600002.SH", "600004.SH", "600005.SH"]
    assert codes(out.missing) == ["600003.SH"]
    assert [f.condition_id for f in out.funnel] == [None, "c2", "c3", "c1"]
    assert [f.remaining for f in out.funnel] == [5, 3, 2, 1]
    assert evals(out)["600002.SH"].status == "skipped"
    excluded = {s.thscode: s for s in out.excluded}
    assert excluded["600002.SH"].failed_conditions == ["c2"]
    assert excluded["600002.SH"].missing_conditions == []  # 未取数不算该股的缺失原因
    assert out.missing[0].missing_conditions == ["c1", "c2"]  # 无 fail 时，未取数与缺失都要列出
    cheap = stat(out, "c2")
    assert cheap.drop_effect is None and "预筛模式" in cheap.note
    deep_stat = stat(out, "c1")
    assert deep_stat.drop_effect == 2 and deep_stat.skipped == 3
    assert "3 只股票未评估" in deep_stat.note
    assert out.conflicts == []  # 覆盖率按已取数的股票计算，skipped 不算缺失


def test_prefiltered_skip_reason_distinguishes_failed_and_missing_cheap_conditions():
    stocks = [
        make_stock("600001.SH", {"pe_ttm": 8.0, "is_st": False, "roe": 12.0, "dividend_yield_ttm": 2.0}),  # 通过预筛，已深度取数
        make_stock("600002.SH", {"pe_ttm": 25.0, "is_st": False}),  # 前置条件未通过
        make_stock("600003.SH", {"pe_ttm": None, "is_st": False}),  # 前置条件数据缺失
        make_stock("600004.SH", {"pe_ttm": 30.0, "is_st": None}),  # 既有未通过又有缺失 → 按“被排除”
        make_stock("600005.SH", {"pe_ttm": not_applicable("pe_ttm"), "is_st": False}),  # 前置条件不适用
        make_stock("600006.SH", {"pe_ttm": 40.0, "is_st": True}),  # 两条前置条件都未通过
    ]
    s = make_strategy(C("pe_ttm", "between", 0, 10), C("is_st", "is_false"), C("roe", "gte", 10),
                      C("dividend_yield_ttm", "gte", 1, hard=False))
    out = evaluate(s, make_snapshot(stocks, prefiltered=True))
    pe_label, st_label = stat(out, "c1").label, stat(out, "c2").label
    for cid in ("c3", "c4"):  # 深度硬条件与软条件的原因一致
        ev = evals(out, cid)
        assert ev["600001.SH"].status == "pass"
        assert ev["600002.SH"].status == "skipped"
        assert ev["600002.SH"].reason == f"已被前置条件排除（未通过：「{pe_label}」），未取数"
        assert ev["600003.SH"].reason == f"前置条件数据缺失（「{pe_label}」），未进入深度评估"
        assert ev["600004.SH"].reason == f"已被前置条件排除（未通过：「{pe_label}」），未取数"
        assert ev["600005.SH"].reason == f"前置条件数据缺失或不适用（「{pe_label}」），未进入深度评估"
        assert ev["600006.SH"].reason == f"已被前置条件排除（未通过：「{pe_label}」、「{st_label}」），未取数"
    assert codes(out.included) == ["600001.SH"]
    assert codes(out.excluded) == ["600002.SH", "600004.SH", "600006.SH"]
    assert codes(out.missing) == ["600003.SH", "600005.SH"]
    deep = stat(out, "c3")
    assert deep.skipped == 5 and "该条件对 5 只股票未评估" in deep.note
    # 深度条件对全部股票都未评估时，统计说明写明“全部”
    all_skipped = evaluate(make_strategy(C("pe_ttm", "between", 0, 10), C("roe", "gte", 10)),
                           make_snapshot([make_stock("600002.SH", {"pe_ttm": 25.0}), make_stock("600003.SH", {"pe_ttm": None})], prefiltered=True))
    note = stat(all_skipped, "c2").note
    assert "全部 2 只股票未评估" in note and stat(all_skipped, "c2").passed == stat(all_skipped, "c2").failed == 0
    # 未通过的前置条件较多时只列前 3 条
    many = evaluate(make_strategy(C("pe_ttm", "between", 0, 10), C("pb_mrq", "lt", 1), C("ps_ttm", "lt", 1), C("is_st", "is_false"), C("roe", "gte", 10)),
                    make_snapshot([make_stock("600002.SH", {"pe_ttm": 30.0, "pb_mrq": 5.0, "ps_ttm": 5.0, "is_st": True})], prefiltered=True))
    labels = [stat(many, f"c{k}").label for k in (1, 2, 3)]
    assert evals(many, "c5")["600002.SH"].reason == f"已被前置条件排除（未通过：「{labels[0]}」、「{labels[1]}」、「{labels[2]}」 等 4 条），未取数"
    # 非预筛快照不受影响
    plain = evaluate(make_strategy(C("pe_ttm", "between", 0, 10), C("roe", "gte", 10)), make_snapshot([make_stock("600002.SH", {"pe_ttm": 25.0})]))
    assert evals(plain, "c2")["600002.SH"].reason == "未获取该指标" and stat(plain, "c2").note is None


# ───────────────────────────── 性能 ─────────────────────────────


def test_performance_1000_stocks_15_conditions():
    snap = synthetic_snapshot(1000, seed=11)
    s = make_strategy(
        C("is_st", "is_false"), C("listing_days", "gte", 365), C("pe_ttm", "gt", 0),
        C("pe_ttm", "pct_rank_lte", 60, scope="industry"), C("pb_mrq", "pct_rank_lte", 70, scope="market"),
        C("revenue_yoy", "gt", -10), C("net_profit_yoy", "gt", -20), C("roe_annualized", "gte", 2),
        C("debt_ratio", "lte", 80), C("volatility_250d", "pct_rank_lte", 80, scope="universe"),
        C("max_drawdown_250d", "lte", 55), C("avg_amount_20d", "gte", 0.3), C("return_60d", "between", -40, 40),
        C("gross_margin", "gte", 15, hard=False), C("dividend_yield_ttm", "gte", 1, hard=False),
    )
    t0 = time.perf_counter()
    out = evaluate(s, snap)
    elapsed = time.perf_counter() - t0
    assert len(out.condition_stats) == 15
    assert all(st.sweep for st in out.condition_stats if st.hard and st.condition_id not in ("c1",))
    assert len(all_results(out)) == 1000
    assert elapsed < 3.0, f"1000 只 × 15 条件评估耗时 {elapsed:.2f}s"
