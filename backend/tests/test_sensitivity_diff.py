"""engine/sensitivity.py 与 engine/diff.py：阈值扫描/去条件影响与暴力重算一致；diff 的逐股原因与策略变化文本。"""

from __future__ import annotations

import pytest

from app.engine.conditions import normalize
from app.engine.diff import diff_results
from app.engine.evaluate import evaluate
from app.engine.sensitivity import drop_effect, sweep_values_for, threshold_sweep
from app.schemas import Strategy, Universe
from tests._fixtures_engine import make_condition as C
from tests._fixtures_engine import make_snapshot, make_stock, make_strategy, synthetic_snapshot


def variant(s: Strategy, cid: str, **changes) -> Strategy:
    ns = normalize(s).model_copy(deep=True)
    for c in ns.conditions:
        if c.id == cid:
            for k, v in changes.items():
                setattr(c, k, v)
    return ns


def without(s: Strategy, cid: str) -> Strategy:
    ns = normalize(s).model_copy(deep=True)
    ns.conditions = [c for c in ns.conditions if c.id != cid]
    return ns


def n_included(s: Strategy, snap) -> int:
    return len(evaluate(s, snap, compute_stats=False).included)


# ───────────────────────────── 扫描点 ─────────────────────────────


@pytest.mark.parametrize(
    ("cond", "expected"),
    [
        (C("pe_ttm", "lte", 25), [12.5, 15, 18.75, 20, 25, 30, 31.25, 37.5, 40]),
        (C("revenue_yoy", "gt", 0), [-10, 0, 5, 10, 15, 20, 30, 50]),
        (C("pe_ttm", "between", 10, 30), [15, 20, 22.5, 25, 30, 37.5, 40, 45, 50]),  # 扫上界，且 ≥ 下界
        (C("debt_ratio", "lte", 250), [30, 40, 50, 60, 70, 80, 125, 187.5, 250]),  # 超出 plausible_range 的点被剔除
        (C("debt_ratio", "lte", 600), [30, 40, 50, 60, 70, 80, 300, 600]),  # 当前值即使越界也保留
        (C("pe_ttm", "pct_rank_lte", 50, scope="industry"), [20, 30, 40, 50, 60, 70, 80]),
        (C("pe_ttm", "pct_rank_gte", 35, scope="industry"), [20, 30, 35, 40, 50, 60, 70, 80]),
        (C("is_st", "is_false"), []),
    ],
)
def test_sweep_values_for(cond, expected):
    assert sweep_values_for(cond) == [float(x) for x in expected]


# ───────────────────────────── threshold_sweep / drop_effect ─────────────────────────────


def _sens_case(policy: str = "exclude"):
    snap = synthetic_snapshot(150, seed=21, metrics=["pe_ttm", "roe_annualized", "debt_ratio", "volatility_250d", "is_st"],
                              missing_rate=0.08)
    s = make_strategy(C("pe_ttm", "gt", 0), C("roe_annualized", "gte", 8), C("debt_ratio", "between", 0, 60),
                      C("volatility_250d", "pct_rank_lte", 50, scope="universe"), C("is_st", "is_false"),
                      C("roe_annualized", "gte", 15, hard=False), policy=policy)
    return snap, s


@pytest.mark.parametrize("policy", ["exclude", "keep_flagged"])
def test_threshold_sweep_matches_brute_force(policy):
    snap, s = _sens_case(policy)
    pts = threshold_sweep(s, snap, "c2", [0, 4, 8, 12, 16])
    assert [p.is_current for p in pts] == [False, False, True, False, False]
    for p in pts:
        assert p.included == n_included(variant(s, "c2", value=p.value), snap)
    for p in threshold_sweep(s, snap, "c3", [20, 40, 60, 80]):  # between 扫上界
        assert p.included == n_included(variant(s, "c3", value2=p.value), snap)
    for p in threshold_sweep(s, snap, "c4", [20, 50, 80]):  # 分位
        assert p.included == n_included(variant(s, "c4", value=p.value), snap)
    soft = threshold_sweep(s, snap, "c6", [5, 10, 20])  # 软条件不影响入选数
    assert {p.included for p in soft} == {n_included(s, snap)}


@pytest.mark.parametrize("policy", ["exclude", "keep_flagged"])
def test_drop_effect_matches_brute_force(policy):
    snap, s = _sens_case(policy)
    out = evaluate(s, snap)
    for cid in ("c1", "c2", "c3", "c4", "c5", "c6"):
        expected = n_included(without(s, cid), snap)
        assert drop_effect(s, snap, cid) == expected
        assert next(st.drop_effect for st in out.condition_stats if st.condition_id == cid) == expected


def test_disabled_condition_is_swept_as_if_enabled():
    snap, s = _sens_case()
    disabled = variant(s, "c3", enabled=False)
    [p] = threshold_sweep(disabled, snap, "c3", [60])
    assert p.included == n_included(s, snap) and p.is_current
    assert drop_effect(disabled, snap, "c3") == n_included(disabled, snap)


def test_sweep_errors():
    snap, s = _sens_case()
    with pytest.raises(ValueError):
        threshold_sweep(s, snap, "c5", [1])  # 布尔条件
    with pytest.raises(KeyError):
        threshold_sweep(s, snap, "nope", [1])
    with pytest.raises(KeyError):
        drop_effect(s, snap, "nope")


# ───────────────────────────── diff ─────────────────────────────


def _diff_snapshot(overrides: dict | None = None, codes: list[str] | None = None):
    rows = {
        "600001.SH": {"pe_ttm": 10.0, "revenue_yoy": 5.0, "gross_margin": 45.0},
        "600002.SH": {"pe_ttm": 22.5, "revenue_yoy": 8.0, "gross_margin": 50.0},
        "600003.SH": {"pe_ttm": 15.0, "revenue_yoy": -3.0, "gross_margin": 30.0},
        "600004.SH": {"pe_ttm": 12.0, "revenue_yoy": 6.0, "gross_margin": None},
        "600005.SH": {"pe_ttm": 30.0, "revenue_yoy": 9.0, "gross_margin": 60.0},
    }
    for code, patch in (overrides or {}).items():
        rows[code] = {**rows[code], **patch}
    keep = codes or list(rows)
    return make_snapshot([make_stock(c, rows[c], name=f"样例{c[3:6]}") for c in keep])


def _diff(base: Strategy, new: Strategy, base_snap=None, new_snap=None, **kw):
    base_snap = base_snap or _diff_snapshot()
    new_snap = new_snap or base_snap
    return diff_results(evaluate(base, base_snap), evaluate(new, new_snap), base, new, **kw)


def test_diff_relaxed_threshold_names_value_and_both_thresholds():
    base = make_strategy(C("pe_ttm", "lte", 20, cid="c1"))
    new = make_strategy(C("pe_ttm", "lte", 25, cid="c1"))
    d = _diff(base, new, base_run_id="r1", new_run_id="r2")
    assert (d.base_run_id, d.new_run_id) == ("r1", "r2")
    assert [a.thscode for a in d.added] == ["600002.SH"] and d.removed == [] and d.unchanged == 3
    assert d.added[0].name == "样例002"
    reason = d.added[0].reason
    assert "22.50 倍" in reason and "≤ 20 倍" in reason and "≤ 25 倍" in reason
    assert "旧阈值 ≤ 20 倍 未满足" in reason and "新阈值 ≤ 25 倍 满足" in reason
    assert d.changes_text == ["修改条件：市盈率TTM ≤ 20 倍 → 市盈率TTM ≤ 25 倍"]
    tightened = _diff(new, base)
    assert [r.thscode for r in tightened.removed] == ["600002.SH"]
    assert "旧阈值 ≤ 25 倍 满足，新阈值 ≤ 20 倍 不满足" in tightened.removed[0].reason


def test_diff_added_and_deleted_condition():
    base = make_strategy(C("pe_ttm", "lte", 25, cid="c1"))
    new = make_strategy(C("pe_ttm", "lte", 25, cid="c1"), C("revenue_yoy", "gt", 0, cid="c2"))
    d = _diff(base, new)
    assert [r.thscode for r in d.removed] == ["600003.SH"]
    assert d.removed[0].reason == "新增条件「营业收入同比增长率 > 0%」未通过：实际 -3.00%，要求 > 0%"
    assert d.changes_text == ["新增条件：营业收入同比增长率 > 0%"]
    back = _diff(new, base)
    assert [a.thscode for a in back.added] == ["600003.SH"]
    assert back.added[0].reason.startswith("删除了条件「营业收入同比增长率 > 0%」（该股原先未通过：实际 -3.00%")
    assert back.changes_text == ["删除条件：营业收入同比增长率 > 0%"]


def test_diff_disable_and_soft_to_hard():
    base = make_strategy(C("pe_ttm", "lte", 25, cid="c1"), C("revenue_yoy", "gt", 0, cid="c2"))
    d = _diff(base, variant(base, "c2", enabled=False))
    assert [a.thscode for a in d.added] == ["600003.SH"] and d.added[0].reason.startswith("停用了条件")
    assert d.changes_text == ["停用条件：营业收入同比增长率 > 0%"]
    soft = variant(base, "c2", hard=False)
    d2 = _diff(soft, base)
    assert [r.thscode for r in d2.removed] == ["600003.SH"]
    assert "由软条件改为硬条件后未通过：实际 -3.00%" in d2.removed[0].reason
    assert d2.changes_text == ["「营业收入同比增长率 > 0%」由软条件改为硬条件（参与过滤）"]


def test_diff_missing_policy_change():
    base = make_strategy(C("pe_ttm", "lte", 25, cid="c1"), C("gross_margin", "gte", 30, cid="c2"))
    new = make_strategy(C("pe_ttm", "lte", 25, cid="c1"), C("gross_margin", "gte", 30, cid="c2"), policy="keep_flagged")
    d = _diff(base, new)
    assert [a.thscode for a in d.added] == ["600004.SH"]
    reason = d.added[0].reason
    assert reason.startswith("缺失数据处理改为“保留并标记”") and "接口返回空值" in reason and "带标记入选" in reason
    assert "入选带缺失标记" not in reason  # 不重复说明
    assert d.changes_text == ["缺失数据处理：剔除（进入“数据缺失”分组） → 保留并标记"]
    back = _diff(new, base)
    assert back.removed[0].reason.startswith("缺失数据处理改为“剔除”")


def test_diff_universe_change():
    pool_a = Universe(kind="custom", code=None, name="池A", codes=["600001.SH", "600002.SH", "600003.SH"])
    pool_b = Universe(kind="custom", code=None, name="池B", codes=["600001.SH", "600002.SH", "600003.SH", "600004.SH"])
    base = make_strategy(C("pe_ttm", "lte", 25, cid="c1"), universe=pool_a)
    new = make_strategy(C("pe_ttm", "lte", 25, cid="c1"), universe=pool_b)
    d = _diff(base, new, base_snap=_diff_snapshot(codes=pool_a.codes), new_snap=_diff_snapshot(codes=pool_b.codes))
    assert [a.thscode for a in d.added] == ["600004.SH"]
    assert d.added[0].reason == "股票池改为「池B」后新纳入该股（原股票池不含），且满足全部硬条件"
    assert d.changes_text == ["股票池：池A → 池B"]
    back = _diff(new, base, base_snap=_diff_snapshot(codes=pool_b.codes), new_snap=_diff_snapshot(codes=pool_a.codes))
    assert back.removed[0].reason == "股票池改为「池A」后不再包含该股"


def test_diff_data_update_with_same_strategy():
    s = make_strategy(C("pe_ttm", "lte", 20, cid="c1"))
    new_snap = _diff_snapshot({"600002.SH": {"pe_ttm": 18.0}, "600001.SH": {"pe_ttm": None}})
    d = _diff(s, s, new_snap=new_snap)
    assert [a.thscode for a in d.added] == ["600002.SH"]
    assert d.added[0].reason == "条件「市盈率TTM ≤ 20 倍」：该股数值由 22.50 倍 变为 18.00 倍，现满足 ≤ 20 倍"
    assert [r.thscode for r in d.removed] == ["600001.SH"]
    assert d.removed[0].reason.startswith("条件「市盈率TTM ≤ 20 倍」原先通过（实际 10.00 倍），现数据缺失")
    assert d.changes_text == ["策略未变化，结果差异来自数据更新（数值或数据可用性变化）"]


def test_diff_no_change_and_renamed_ids():
    s = make_strategy(C("pe_ttm", "lte", 20, cid="a"))
    renamed = make_strategy(C("pe_ttm", "lte", 20, cid="b"), name="改了名字")
    d = _diff(s, renamed)
    assert d.added == [] and d.removed == [] and d.unchanged == 3
    assert d.changes_text == ["策略未变化"]


def test_diff_rank_condition_reason_mentions_rank():
    snap = make_snapshot([make_stock(f"6000{i:02d}.SH", {"volatility_250d": float(10 + i)}) for i in range(11)])
    base = make_strategy(C("volatility_250d", "pct_rank_lte", 50, scope="universe", cid="c1"))
    new = make_strategy(C("volatility_250d", "pct_rank_lte", 60, scope="universe", cid="c1"))
    d = _diff(base, new, base_snap=snap)
    assert [a.thscode for a in d.added] == ["600006.SH"]
    assert "分位 60%（16.00%）" in d.added[0].reason and "股票池内分位 ≤ 50%" in d.added[0].reason
