"""engine/conflicts.py：矛盾、冗余、越界、非法算子、张力、空硬条件与 validate_strategy。"""

from __future__ import annotations

from app.engine.conflicts import detect_conflicts, validate_strategy
from app.schemas import Universe
from tests._fixtures_engine import make_condition as C
from tests._fixtures_engine import make_strategy

HS300 = Universe(kind="index", code="000300.SH", name="沪深300")
ZZ1000 = Universe(kind="index", code="000852.SH", name="中证1000")
BANK = Universe(kind="industry", code="881155.TI", name="银行")
BROKER = Universe(kind="industry", code="881157.TI", name="证券")


def kinds(conflicts):
    return [(c.severity, c.kind) for c in conflicts]


def by_kind(conflicts, kind):
    return [c for c in conflicts if c.kind == kind]


# ───────────────────────────── 矛盾 ─────────────────────────────


def test_numeric_contradiction_is_error_and_blocks_run():
    s = make_strategy(C("pe_ttm", "gt", 30, cid="a"), C("pe_ttm", "lt", 20, cid="b"))
    res = validate_strategy(s)
    [c] = by_kind(res.conflicts, "contradiction")
    assert c.severity == "error" and c.condition_ids == ["a", "b"]
    assert "市盈率TTM > 30 倍" in c.message and "市盈率TTM < 20 倍" in c.message
    assert c.suggestion and c.source == "rule"
    assert res.can_run is False


def test_open_endpoints_contradict_but_single_point_does_not():
    assert by_kind(detect_conflicts(make_strategy(C("pe_ttm", "gt", 10), C("pe_ttm", "lte", 10))), "contradiction")
    assert not by_kind(detect_conflicts(make_strategy(C("pe_ttm", "gte", 10), C("pe_ttm", "lte", 10))), "contradiction")


def test_rank_contradiction_only_within_same_scope():
    same = make_strategy(C("pe_ttm", "pct_rank_lte", 30, scope="industry"),
                         C("pe_ttm", "pct_rank_gte", 70, scope="industry"))
    [c] = by_kind(detect_conflicts(same), "contradiction")
    assert c.severity == "error" and "分位区间" in c.message
    diff_scope = make_strategy(C("pe_ttm", "pct_rank_lte", 30, scope="industry"),
                               C("pe_ttm", "pct_rank_gte", 70, scope="market"))
    assert not by_kind(detect_conflicts(diff_scope), "contradiction")


def test_numeric_and_rank_conditions_are_not_compared():
    s = make_strategy(C("pe_ttm", "gt", 80), C("pe_ttm", "pct_rank_lte", 20, scope="industry"))
    assert not by_kind(detect_conflicts(s), "contradiction")
    assert not by_kind(detect_conflicts(s), "redundant")


def test_bool_contradiction():
    s = make_strategy(C("is_st", "is_true"), C("is_st", "is_false"))
    [c] = by_kind(detect_conflicts(s), "contradiction")
    assert c.severity == "error"


def test_disabled_conditions_do_not_participate():
    s = make_strategy(C("pe_ttm", "gt", 30), C("pe_ttm", "lt", 20, enabled=False), C("no_such", "gt", 1, enabled=False))
    res = validate_strategy(s)
    assert res.can_run is True
    assert not by_kind(res.conflicts, "contradiction") and not by_kind(res.conflicts, "unsupported_metric")
    assert any("停用" in w for w in res.warnings)


def test_soft_contradiction_is_warning():
    s = make_strategy(C("pe_ttm", "gt", 30), C("pe_ttm", "lt", 20, hard=False))
    [c] = by_kind(detect_conflicts(s), "contradiction")
    assert c.severity == "warning" and "计分恒为 0" in c.message
    assert validate_strategy(s).can_run is True


# ───────────────────────────── 冗余 ─────────────────────────────


def test_redundant_wider_condition_is_named():
    s = make_strategy(C("revenue_yoy", "gt", 0, cid="wide"), C("revenue_yoy", "gte", 20, cid="narrow"))
    [c] = by_kind(detect_conflicts(s), "redundant")
    assert c.severity == "info" and c.condition_ids == ["wide", "narrow"]
    assert c.message.startswith("「营业收入同比增长率 > 0%」已被更严格的「营业收入同比增长率 ≥ 20%」覆盖")


def test_identical_conditions_are_redundant():
    s = make_strategy(C("debt_ratio", "lte", 60), C("debt_ratio", "lte", 60))
    [c] = by_kind(detect_conflicts(s), "redundant")
    assert "等价" in c.message


def test_hard_filter_plus_stricter_soft_preference_is_not_redundant():
    s = make_strategy(C("revenue_yoy", "gt", 0), C("revenue_yoy", "gte", 20, hard=False))
    assert not by_kind(detect_conflicts(s), "redundant")
    covered_soft = make_strategy(C("revenue_yoy", "gte", 20), C("revenue_yoy", "gt", 0, hard=False))
    [c] = by_kind(detect_conflicts(covered_soft), "redundant")
    assert "恒成立" in c.message


# ───────────────────────────── 越界 / 非法 ─────────────────────────────


def test_threshold_outside_plausible_range_warns():
    s = make_strategy(C("debt_ratio", "lte", 600))
    [c] = by_kind(detect_conflicts(s), "implausible")
    assert c.severity == "warning" and "600%" in c.message and "[0, 300]%" in c.message
    assert validate_strategy(s).can_run is True


def test_reversed_between_warns():
    [c] = by_kind(detect_conflicts(make_strategy(C("pe_ttm", "between", 25, 5))), "implausible")
    assert "下界 25 大于上界 5" in c.message


def test_unsupported_metric_op_rank_and_scope_are_errors():
    s = make_strategy(
        C("no_such_metric", "gt", 1, cid="u1"),
        C("is_st", "gt", 1, cid="u2"),
        C("pe_ttm", "pct_rank_lte", 120, scope="industry", cid="u3"),
        C("pe_ttm", "pct_rank_lte", 50, cid="u4"),
        C("revenue_yoy", "pct_rank_gte", 70, scope="industry", cid="u5"),
        C("pe_ttm", "gt", None, cid="u6"),
    )
    res = validate_strategy(s)
    errs = by_kind(res.conflicts, "unsupported_metric")
    assert {c.condition_ids[0] for c in errs} == {"u1", "u2", "u3", "u4", "u5", "u6"}
    assert all(c.severity == "error" and c.suggestion for c in errs)
    assert res.can_run is False
    u2 = next(c for c in errs if c.condition_ids == ["u2"])
    assert "不支持算子「大于」" in u2.message and "是、否" in u2.suggestion
    u5 = next(c for c in errs if c.condition_ids == ["u5"])
    assert "所属行业内" in u5.message and "股票池内" in u5.suggestion


def test_negative_soft_weight_is_error():
    s = make_strategy(C("pe_ttm", "gt", 0), C("roe", "gt", 5, hard=False, weight=-1))
    res = validate_strategy(s)
    assert ("error", "implausible") in kinds(res.conflicts) and res.can_run is False


# ───────────────────────────── 张力 ─────────────────────────────


def test_tension_high_growth_with_low_valuation():
    s = make_strategy(C("net_profit_yoy", "gte", 30, cid="g"), C("pe_ttm", "lte", 12, cid="v"))
    [c] = by_kind(detect_conflicts(s), "tension")
    assert c.severity == "warning" and c.condition_ids == ["g", "v"]
    assert "高成长与低估值" in c.message and "净利润同比增长率 ≥ 30%" in c.message and "市盈率TTM ≤ 12 倍" in c.message
    by_rank = make_strategy(C("revenue_yoy", "pct_rank_gte", 80, scope="universe"),
                            C("pe_ttm", "pct_rank_lte", 20, scope="industry"))
    assert by_kind(detect_conflicts(by_rank), "tension")
    mild = make_strategy(C("net_profit_yoy", "gte", 20), C("pe_ttm", "lte", 12))
    assert not by_kind(detect_conflicts(mild), "tension")
    soft = make_strategy(C("net_profit_yoy", "gte", 30, hard=False), C("pe_ttm", "lte", 12))
    assert not by_kind(detect_conflicts(soft), "tension")


def test_tension_low_volatility_with_strong_momentum():
    s = make_strategy(C("volatility_250d", "pct_rank_lte", 30, scope="universe"), C("return_60d", "gte", 20))
    [c] = by_kind(detect_conflicts(s), "tension")
    assert "低波动与强动量" in c.message
    s2 = make_strategy(C("volatility_250d", "lte", 20), C("return_20d", "gt", 15))
    assert by_kind(detect_conflicts(s2), "tension")
    s3 = make_strategy(C("volatility_250d", "lte", 35), C("return_20d", "gt", 15))
    assert not by_kind(detect_conflicts(s3), "tension")


def test_small_cap_in_large_cap_index_pool():
    s = make_strategy(C("market_cap_est", "lte", 100), universe=HS300)
    [c] = by_kind(detect_conflicts(s), "universe_mismatch")
    assert c.severity == "warning" and "沪深300" in c.message and "中证1000" in c.suggestion
    assert not by_kind(detect_conflicts(make_strategy(C("market_cap_est", "lte", 100), universe=ZZ1000)), "universe_mismatch")


def test_debt_ratio_in_financial_pool_and_gross_margin_in_bank_pool():
    debt = make_strategy(C("debt_ratio", "lte", 60), universe=BROKER)
    [c] = by_kind(detect_conflicts(debt), "universe_mismatch")
    assert "资产负债率" in c.message and "证券" in c.message
    # 池内分位是推荐的替代做法，不应再提示
    assert not by_kind(detect_conflicts(make_strategy(
        C("debt_ratio", "pct_rank_lte", 50, scope="universe"), universe=BANK)), "universe_mismatch")
    gm = make_strategy(C("gross_margin", "gte", 40), universe=BANK)
    [c2] = by_kind(detect_conflicts(gm), "universe_mismatch")
    assert "毛利率" in c2.message and "银行" in c2.message
    assert not by_kind(detect_conflicts(make_strategy(C("gross_margin", "gte", 40), universe=BROKER)), "universe_mismatch")


# ───────────────────────────── 空硬条件 / validate ─────────────────────────────


def test_empty_hard_warning():
    soft_only = make_strategy(C("roe", "gt", 5, hard=False))
    [c] = by_kind(detect_conflicts(soft_only), "empty_hard")
    assert c.severity == "warning"
    assert by_kind(detect_conflicts(make_strategy()), "empty_hard")
    assert not by_kind(detect_conflicts(make_strategy(C("roe", "gt", 5))), "empty_hard")


def test_validate_normalizes_and_orders_by_severity():
    s = make_strategy(C("revenue_yoy", "gt", 0), C("revenue_yoy", "gte", 20), C("pe_ttm", "gt", 30), C("pe_ttm", "lt", 20))
    res = validate_strategy(s)
    assert [c.id for c in res.strategy.conditions] == ["c1", "c2", "c3", "c4"]
    assert all(c.label for c in res.strategy.conditions)
    sev = [c.severity for c in res.conflicts]
    assert sev == sorted(sev, key=["error", "warning", "info"].index)
    assert len({c.id for c in res.conflicts}) == len(res.conflicts)


def test_clean_strategy_can_run_and_is_deterministic():
    s = make_strategy(C("is_st", "is_false"), C("listing_days", "gte", 365), C("pe_ttm", "between", 0, 25),
                      C("roe_annualized", "gte", 10, hard=False))
    a, b = validate_strategy(s), validate_strategy(s)
    assert a.can_run is True and a.conflicts == []
    assert a.model_dump() == b.model_dump()


def test_unresolved_universe_and_unknown_sort_metric_warn():
    s = make_strategy(C("pe_ttm", "gt", 0), universe=Universe(kind="industry", code=None, name=None, query="白酒"),
                      sort_metric="no_such")
    res = validate_strategy(s)
    assert any("白酒" in w for w in res.warnings) and any("no_such" in w for w in res.warnings)
