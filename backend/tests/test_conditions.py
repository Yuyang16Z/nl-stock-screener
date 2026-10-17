"""engine/conditions.py：描述文本、阈值文本、区间、规范化、哈希、澄清应用。"""

from __future__ import annotations

import math

import pytest

from app.engine.conditions import (
    DEFAULT_SYSTEM_CONDITIONS,
    apply_clarification,
    contains,
    default_system_conditions,
    describe,
    fmt_num,
    interval,
    intersect,
    normalize,
    semantic_key,
    strategy_hash,
    structural_problems,
    threshold_text,
)
from app.schemas import Clarification, ClarificationOption, Universe
from tests._fixtures_engine import make_condition as C
from tests._fixtures_engine import make_strategy

INF = math.inf


# ───────────────────────────── describe / threshold_text ─────────────────────────────


@pytest.mark.parametrize(
    ("cond", "text"),
    [
        (C("pe_ttm", "pct_rank_lte", 50, scope="industry"), "市盈率TTM 在所属行业内分位 ≤ 50%"),
        (C("revenue_yoy", "gt", 0), "营业收入同比增长率 > 0%"),
        (C("pe_ttm", "between", 0, 25), "市盈率TTM 介于 0~25 倍"),
        (C("pe_ttm", "between", 25, 0), "市盈率TTM 介于 0~25 倍"),  # 上下界颠倒时按语义展示
        (C("is_st", "is_false"), "是否ST：否"),
        (C("is_st", "is_true"), "是否ST：是"),
        (C("listing_days", "gte", 365), "上市天数 ≥ 365 天"),
        (C("market_cap_est", "lte", 100), "估算总市值 ≤ 100 亿元"),
        (C("pb_mrq", "lt", 1.5), "市净率MRQ < 1.5 倍"),
        (C("return_60d", "lte", -20), "近60日涨跌幅 ≤ -20%"),
        (C("roe_change", "gt", 0.5), "ROE同比变动 > 0.5 百分点"),
        (C("beta_250d", "lte", 0.8), "Beta（近250日，相对沪深300） ≤ 0.8"),
        (C("volatility_250d", "pct_rank_gte", 70, scope="universe"), "年化波动率（近250日） 在股票池内分位 ≥ 70%"),
        (C("pe_ttm", "pct_rank_lte", 30, scope="market"), "市盈率TTM 在全A股内分位 ≤ 30%"),
        (C("unknown_metric", "gt", 3), "unknown_metric > 3"),
    ],
)
def test_describe(cond, text):
    assert describe(cond) == text


@pytest.mark.parametrize(
    ("cond", "text"),
    [
        (C("revenue_yoy", "gt", 0), "> 0%"),
        (C("pe_ttm", "pct_rank_lte", 50, scope="industry"), "行业内分位 ≤ 50%"),
        (C("roe_annualized", "pct_rank_gte", 70, scope="universe"), "股票池内分位 ≥ 70%"),
        (C("pe_ttm", "pct_rank_lte", 30, scope="market"), "全A股内分位 ≤ 30%"),
        (C("pe_ttm", "between", 0, 25), "∈ [0, 25] 倍"),
        (C("return_20d", "between", -10, 10), "∈ [-10, 10]%"),
        (C("listing_days", "gte", 365), "≥ 365 天"),
        (C("debt_ratio", "lte", 60), "≤ 60%"),
        (C("is_st", "is_false"), "否"),
        (C("is_st", "is_true"), "是"),
    ],
)
def test_threshold_text(cond, text):
    assert threshold_text(cond) == text


def test_fmt_num_trims_trailing_zeros():
    assert fmt_num(25.0) == "25"
    assert fmt_num(1.50) == "1.5"
    assert fmt_num(18.75) == "18.75"
    assert fmt_num(-0.0) == "0"
    assert fmt_num(300000) == "300000"
    assert fmt_num(None) == "?"


# ───────────────────────────── 区间 ─────────────────────────────


def test_interval_by_op():
    assert interval(C("pe_ttm", "gt", 10)) == (10.0, False, INF, False)
    assert interval(C("pe_ttm", "gte", 10)) == (10.0, True, INF, False)
    assert interval(C("pe_ttm", "lt", 10)) == (-INF, False, 10.0, False)
    assert interval(C("pe_ttm", "lte", 10)) == (-INF, False, 10.0, True)
    assert interval(C("pe_ttm", "between", 25, 5)) == (5.0, True, 25.0, True)
    assert interval(C("pe_ttm", "pct_rank_lte", 30, scope="industry")) == (0.0, True, 30.0, True)
    assert interval(C("pe_ttm", "pct_rank_gte", 70, scope="industry")) == (70.0, True, 100.0, True)
    assert interval(C("is_st", "is_true")) == (1.0, True, 1.0, True)
    assert interval(C("is_st", "is_false")) == (0.0, True, 0.0, True)
    assert interval(C("pe_ttm", "gt", None)) is None
    assert interval(C("pe_ttm", "between", 1, None)) is None


def test_intersect_and_contains():
    gt10 = interval(C("pe_ttm", "gt", 10))
    lt10 = interval(C("pe_ttm", "lt", 10))
    gte10 = interval(C("pe_ttm", "gte", 10))
    lte10 = interval(C("pe_ttm", "lte", 10))
    assert intersect(gt10, lt10) is None
    assert intersect(gte10, lte10) == (10.0, True, 10.0, True)  # 单点不为空
    assert intersect(gt10, lte10) is None
    assert contains(interval(C("pe_ttm", "gt", 0)), interval(C("pe_ttm", "gte", 20)))
    assert not contains(interval(C("pe_ttm", "gt", 0)), interval(C("pe_ttm", "between", 0, 25)))  # 0 不在 (0, ∞)
    assert contains(gte10, gt10) and not contains(gt10, gte10)


def test_semantic_key_ignores_irrelevant_fields():
    a = C("pe_ttm", "gt", 10, scope="industry", cid="x", hard=False)
    b = C("pe_ttm", "gt", 10, cid="y")
    assert semantic_key(a) == semantic_key(b)
    assert semantic_key(C("pe_ttm", "between", 25, 0)) == semantic_key(C("pe_ttm", "between", 0, 25))
    assert semantic_key(C("pe_ttm", "pct_rank_lte", 50, scope="industry")) != semantic_key(
        C("pe_ttm", "pct_rank_lte", 50, scope="universe"))


def test_structural_problems():
    assert structural_problems(C("pe_ttm", "gt", 10)) == []
    assert [p.code for p in structural_problems(C("no_such", "gt", 1))] == ["unknown_metric"]
    assert [p.code for p in structural_problems(C("is_st", "gt", 1))] == ["op_not_allowed"]
    assert [p.code for p in structural_problems(C("pe_ttm", "gt", None))] == ["missing_value"]
    assert [p.code for p in structural_problems(C("pe_ttm", "gt", float("nan")))] == ["invalid_value"]
    assert [p.code for p in structural_problems(C("pe_ttm", "between", 1, None))] == ["missing_value2"]
    assert [p.code for p in structural_problems(C("pe_ttm", "pct_rank_lte", 120, scope="industry"))] == ["rank_out_of_range"]
    assert [p.code for p in structural_problems(C("pe_ttm", "pct_rank_lte", 50))] == ["missing_scope"]
    assert [p.code for p in structural_problems(C("revenue_yoy", "pct_rank_gte", 70, scope="industry"))] == ["scope_not_allowed"]


# ───────────────────────────── normalize / hash ─────────────────────────────


def test_normalize_fills_ids_and_labels_without_reordering():
    s = make_strategy(
        C("pe_ttm", "gt", 0, cid=""),
        C("pe_ttm", "lt", 30, cid="c1"),
        C("revenue_yoy", "gt", 0, cid="c1"),  # 重复 id
        C("roe", "gt", 5, cid=""),
        C("is_st", "is_false", cid="sys_st"),
    )
    ns = normalize(s)
    assert [c.id for c in ns.conditions] == ["c2", "c1", "c3", "c4", "sys_st"]  # c1 已被占用，跳过
    assert [c.metric for c in ns.conditions] == ["pe_ttm", "pe_ttm", "revenue_yoy", "roe", "is_st"]
    assert ns.conditions[0].label == "市盈率TTM > 0 倍"
    assert all(c.label for c in ns.conditions)
    # 不修改入参；幂等
    assert s.conditions[0].id == "" and s.conditions[0].label is None
    assert normalize(ns).model_dump() == ns.model_dump()


def test_normalize_refreshes_stale_label():
    c = C("pe_ttm", "lte", 30, cid="c1")
    c.label = "市盈率TTM ≤ 20 倍"  # 用户改了阈值但 label 未更新
    assert normalize(make_strategy(c)).conditions[0].label == "市盈率TTM ≤ 30 倍"


def test_strategy_hash_is_stable_and_ignores_label_and_name():
    s = make_strategy(C("pe_ttm", "gt", 0), C("revenue_yoy", "gt", 0), name="A")
    h = strategy_hash(s)
    assert len(h) == 64 and all(ch in "0123456789abcdef" for ch in h)
    assert strategy_hash(s) == h  # 同输入同输出
    assert strategy_hash(normalize(s)) == h  # 规范化前后一致
    renamed = normalize(s).model_copy(deep=True)
    renamed.name = "B"
    renamed.conditions[0].label = "随便写的描述"
    assert strategy_hash(renamed) == h
    changed = normalize(s).model_copy(deep=True)
    changed.conditions[1].value = 5
    assert strategy_hash(changed) != h
    policy = normalize(s).model_copy(deep=True)
    policy.missing_policy = "keep_flagged"
    assert strategy_hash(policy) != h


def test_default_system_conditions_are_copies():
    a = default_system_conditions()
    a[0].value = 999
    assert default_system_conditions()[0].value is None
    assert [c.origin for c in DEFAULT_SYSTEM_CONDITIONS] == ["system", "system"]
    assert {c.metric for c in DEFAULT_SYSTEM_CONDITIONS} == {"is_st", "listing_days"}


# ───────────────────────────── apply_clarification ─────────────────────────────


def _clarification() -> Clarification:
    return Clarification(
        id="q1", phrase="估值合理", question="“估值合理”按哪种口径判断？", default_option_id="a",
        options=[
            ClarificationOption(id="a", label="盈利且 PE 不高于行业中位数", conditions=[
                C("pe_ttm", "gt", 0, origin="ai"), C("pe_ttm", "pct_rank_lte", 50, scope="industry", origin="ai")]),
            ClarificationOption(id="b", label="PE 介于 0~25 倍", conditions=[C("pe_ttm", "between", 0, 25, cid="c1")]),
            ClarificationOption(id="u", label="只看银行", universe=Universe(kind="industry", code="881155.TI", name="银行")),
        ],
    )


def _base_strategy():
    return make_strategy(
        C("revenue_yoy", "gt", 0, cid="c1", origin="ai", source_phrase="经营改善"),
        C("pe_ttm", "gt", 0, cid="c2", origin="ai", source_phrase="估值合理"),
        C("pe_ttm", "pct_rank_lte", 50, scope="industry", cid="c3", origin="ai", source_phrase="估值合理"),
        C("pe_ttm", "lte", 40, cid="c4", origin="user", source_phrase="估值合理"),
        C("is_st", "is_false", cid="sys_st", origin="system", source_phrase="默认风控"),
    )


def test_apply_clarification_replaces_phrase_conditions_in_place():
    s = _base_strategy()
    out = apply_clarification(s, _clarification(), "b")
    assert [(c.metric, c.op) for c in out.conditions] == [
        ("revenue_yoy", "gt"), ("pe_ttm", "between"), ("pe_ttm", "lte"), ("is_st", "is_false")]
    inserted = out.conditions[1]
    assert inserted.source_phrase == "估值合理" and inserted.origin == "ai"  # 继承被替换条件的来源
    assert inserted.id not in ("c1", "c4", "sys_st")  # 与已有 id 冲突时重新编号
    assert [c.id for c in out.conditions if c.id in ("c1", "c4", "sys_st")] == ["c1", "c4", "sys_st"]
    assert inserted.label == "市盈率TTM 介于 0~25 倍"
    assert len(s.conditions) == 5  # 入参不变


def test_apply_clarification_can_switch_options_repeatedly():
    cl = _clarification()
    s = _base_strategy()
    via_b = apply_clarification(apply_clarification(s, cl, "b"), cl, "a")
    direct = apply_clarification(s, cl, "a")
    assert [semantic_key(c) for c in via_b.conditions] == [semantic_key(c) for c in direct.conditions]


def test_apply_clarification_universe_option_and_unknown_option():
    cl = _clarification()
    out = apply_clarification(_base_strategy(), cl, "u")
    assert out.universe.name == "银行" and out.universe.kind == "industry"
    assert [c.id for c in out.conditions] == ["c1", "c4", "sys_st"]  # 该短语的 ai 条件被删，用户条件保留
    with pytest.raises(ValueError):
        apply_clarification(_base_strategy(), cl, "zzz")


def test_apply_clarification_appends_when_nothing_to_replace():
    cl = _clarification()
    s = make_strategy(C("revenue_yoy", "gt", 0, cid="c1"))
    out = apply_clarification(s, cl, "a")
    assert [c.metric for c in out.conditions] == ["revenue_yoy", "pe_ttm", "pe_ttm"]
    assert all(c.source_phrase == "估值合理" for c in out.conditions[1:])
