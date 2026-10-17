"""两次筛选结果的 diff（DESIGN §5.4）：新增/移除的股票及原因、策略层面的变化。

条件匹配：先按 id（且指标相同）配对，再按“同义”（指标、算子、阈值、分位口径相同）配对，其余视为新增/删除。
每只新增/移除股票的原因只引用两次结果中记录的判定（数值、阈值、状态原因），不做推测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import CondEval, Condition, DiffItem, EvaluationOutput, ResultDiff, StockResult, Strategy, Universe
from .conditions import fmt_num, normalize, semantic_key

_POLICY_LABEL = {"exclude": "剔除（进入“数据缺失”分组）", "keep_flagged": "保留并标记"}


@dataclass
class _Match:
    pairs: list[tuple[Condition, Condition]] = field(default_factory=list)  # (旧, 新)
    removed: list[Condition] = field(default_factory=list)
    added: list[Condition] = field(default_factory=list)
    old_to_new: dict[str, Condition] = field(default_factory=dict)
    new_to_old: dict[str, Condition] = field(default_factory=dict)


def _match_conditions(base: Strategy, new: Strategy) -> _Match:
    m = _Match()
    new_by_id = {c.id: c for c in new.conditions}
    rest_base: list[Condition] = []
    for b in base.conditions:
        n = new_by_id.get(b.id)
        if n is not None and n.metric == b.metric and n.id not in m.new_to_old:
            m.old_to_new[b.id], m.new_to_old[n.id] = n, b
        else:
            rest_base.append(b)
    rest_new = [n for n in new.conditions if n.id not in m.new_to_old]
    for b in rest_base:
        key = semantic_key(b)
        n = next((x for x in rest_new if semantic_key(x) == key), None)
        if n is None:
            m.removed.append(b)
            continue
        rest_new.remove(n)
        m.old_to_new[b.id], m.new_to_old[n.id] = n, b
    m.added = rest_new
    # 按新策略顺序输出配对
    m.pairs = [(m.new_to_old[n.id], n) for n in new.conditions if n.id in m.new_to_old]
    return m


# ───────────────────────────── 策略层面变化 ─────────────────────────────


def _universe_key(u: Universe) -> tuple[object, ...]:
    return (u.kind, u.code, tuple(sorted(u.codes)) if u.kind == "custom" else (), None if u.code else u.query)


def _universe_name(u: Universe) -> str:
    if u.name:
        return u.name
    if u.kind == "all_a":
        return "全部A股"
    if u.kind == "custom":
        return f"自定义（{len(u.codes)} 只）"
    return u.code or u.query or "未命名股票池"


def _q(c: Condition) -> str:
    return f"「{c.label}」"


def _changes_text(base: Strategy, new: Strategy, m: _Match) -> list[str]:
    out: list[str] = []
    if _universe_key(base.universe) != _universe_key(new.universe):
        out.append(f"股票池：{_universe_name(base.universe)} → {_universe_name(new.universe)}")
    for b, n in m.pairs:
        if semantic_key(b) != semantic_key(n):
            out.append(f"修改条件：{b.label} → {n.label}")
        if b.hard != n.hard:
            out.append(f"{_q(n)}由{'硬条件改为软条件（只计分）' if b.hard else '软条件改为硬条件（参与过滤）'}")
        if b.enabled != n.enabled:
            out.append(f"{'启用' if n.enabled else '停用'}条件：{n.label}")
        if not n.hard and b.weight != n.weight:
            out.append(f"{_q(n)}权重：{fmt_num(b.weight)} → {fmt_num(n.weight)}")
    for n in m.added:
        extra = "（软条件）" if not n.hard else ""
        extra += "（未启用）" if not n.enabled else ""
        out.append(f"新增条件：{n.label}{extra}")
    for b in m.removed:
        out.append(f"删除条件：{b.label}")
    if base.missing_policy != new.missing_policy:
        out.append(f"缺失数据处理：{_POLICY_LABEL[base.missing_policy]} → {_POLICY_LABEL[new.missing_policy]}")
    if base.report_period != new.report_period:
        out.append(f"报告期：{base.report_period or '自动'} → {new.report_period or '自动'}")
    if (base.sort_metric, base.sort_desc) != (new.sort_metric, new.sort_desc):
        def sort_text(s: Strategy) -> str:
            return f"{s.sort_metric}（{'降序' if s.sort_desc else '升序'}）" if s.sort_metric else "默认"
        out.append(f"排序：{sort_text(base)} → {sort_text(new)}")
    return out


# ───────────────────────────── 逐股原因 ─────────────────────────────


def _index(out: EvaluationOutput) -> dict[str, StockResult]:
    return {s.thscode: s for s in (*out.included, *out.excluded, *out.missing)}


def _ce(sr: StockResult | None, cond_id: str) -> CondEval | None:
    if sr is None:
        return None
    return next((e for e in sr.evaluations if e.condition_id == cond_id), None)


def _actual(ce: CondEval) -> str:
    if ce.rank_pct is not None:
        return f"分位 {fmt_num(round(ce.rank_pct, 1))}%（{ce.display}）"
    return ce.display


def _detail(ce: CondEval | None) -> str:
    """一条判定的简述，如“未通过：实际 22.5 倍，要求 ≤ 20 倍”。"""
    if ce is None:
        return "无判定记录"
    if ce.status == "pass":
        return f"通过：实际 {_actual(ce)}，要求 {ce.threshold}"
    if ce.status == "fail":
        return f"未通过：实际 {_actual(ce)}，要求 {ce.threshold}"
    if ce.status == "skipped":
        return "未取数（预筛阶段已被其他条件排除）"
    word = "不适用" if ce.status == "not_applicable" else "数据缺失"
    return f"{word}（{ce.reason or '原因未记录'}）"


def _blocking(sr: StockResult) -> list[str]:
    return list(dict.fromkeys([*sr.failed_conditions, *sr.missing_conditions]))


def _added_reason(base_sr: StockResult | None, new_sr: StockResult, base: Strategy, new: Strategy,
                  m: _Match, universe_changed: bool) -> str:
    if base_sr is None:
        if universe_changed:
            return f"股票池改为「{_universe_name(new.universe)}」后新纳入该股（原股票池不含），且满足全部硬条件"
        return "该股此前不在股票池中（成分股变化），现满足全部硬条件"
    base_conds = {c.id: c for c in base.conditions}
    pieces: list[str] = []
    covered: set[str] = set()  # 已在原因里说明过的新条件 id
    for bid in _blocking(base_sr):
        bc = base_conds.get(bid)
        bce = _ce(base_sr, bid)
        if bc is None:
            continue
        nc = m.old_to_new.get(bid)
        if nc is not None:
            covered.add(nc.id)
        if nc is None:
            pieces.append(f"删除了条件{_q(bc)}（该股原先{_detail(bce)}）")
        elif not nc.enabled:
            pieces.append(f"停用了条件{_q(nc)}（该股原先{_detail(bce)}）")
        elif not nc.hard:
            pieces.append(f"条件{_q(nc)}改为软条件（该股原先{_detail(bce)}）")
        else:
            nce = _ce(new_sr, nc.id)
            if nce is None:
                pieces.append(f"条件{_q(nc)}判定发生变化")
            elif nce.status == "pass":
                if semantic_key(bc) != semantic_key(nc):
                    old_t = bce.threshold if bce else bc.label
                    pieces.append(f"条件{_q(bc)}改为{_q(nc)}：该股实际 {_actual(nce)}，"
                                  f"旧阈值 {old_t} 未满足，新阈值 {nce.threshold} 满足")
                elif bce is not None and bce.status == "fail":
                    pieces.append(f"条件{_q(nc)}：该股数值由 {_actual(bce)} 变为 {_actual(nce)}，现满足 {nce.threshold}")
                else:
                    pieces.append(f"条件{_q(nc)}：原先{_detail(bce)}，现为 {_actual(nce)}，满足 {nce.threshold}")
            elif nce.status in ("missing", "not_applicable"):
                if base.missing_policy != new.missing_policy:
                    pieces.append(f"缺失数据处理改为“保留并标记”：条件{_q(nc)}{_detail(nce)}，带标记入选")
                else:
                    pieces.append(f"条件{_q(nc)}：原先{_detail(bce)}，现{_detail(nce)}，按“保留并标记”带标记入选")
            else:
                pieces.append(f"条件{_q(nc)}：原先{_detail(bce)}，现{_detail(nce)}")
    if not pieces:
        pieces.append("判定结果发生变化")
    if new_sr.flagged:
        new_conds = {c.id: c for c in new.conditions}
        flagged = "、".join(_q(new_conds[c]) for c in new_sr.missing_conditions if c in new_conds and c not in covered)
        if flagged:
            pieces.append(f"入选带缺失标记：{flagged}")
    return "；".join(pieces)


def _removed_reason(base_sr: StockResult, new_sr: StockResult | None, base: Strategy, new: Strategy,
                    m: _Match, universe_changed: bool) -> str:
    if new_sr is None:
        if universe_changed:
            return f"股票池改为「{_universe_name(new.universe)}」后不再包含该股"
        return "该股已不在股票池中（成分股变化）"
    new_conds = {c.id: c for c in new.conditions}
    pieces: list[str] = []
    for nid in _blocking(new_sr):
        nc = new_conds.get(nid)
        nce = _ce(new_sr, nid)
        if nc is None:
            continue
        bc = m.new_to_old.get(nid)
        if bc is None:
            pieces.append(f"新增条件{_q(nc)}{_detail(nce)}")
        elif not bc.enabled:
            pieces.append(f"重新启用的条件{_q(nc)}{_detail(nce)}")
        elif not bc.hard:
            pieces.append(f"条件{_q(nc)}由软条件改为硬条件后{_detail(nce)}")
        else:
            bce = _ce(base_sr, bc.id)
            if nce is None:
                pieces.append(f"条件{_q(nc)}判定发生变化")
            elif nce.status == "fail":
                if semantic_key(bc) != semantic_key(nc):
                    old_t = bce.threshold if bce else bc.label
                    pieces.append(f"条件{_q(bc)}改为{_q(nc)}：该股实际 {_actual(nce)}，"
                                  f"旧阈值 {old_t} 满足，新阈值 {nce.threshold} 不满足")
                elif bce is not None and bce.status == "pass":
                    pieces.append(f"条件{_q(nc)}：该股数值由 {_actual(bce)} 变为 {_actual(nce)}，不再满足 {nce.threshold}")
                else:
                    pieces.append(f"条件{_q(nc)}：原先{_detail(bce)}，现{_detail(nce)}")
            else:
                if base.missing_policy != new.missing_policy:
                    pieces.append(f"缺失数据处理改为“剔除”：条件{_q(nc)}{_detail(nce)}")
                elif bce is not None and bce.status == "pass":
                    pieces.append(f"条件{_q(nc)}原先通过（实际 {_actual(bce)}），现{_detail(nce)}")
                else:
                    pieces.append(f"条件{_q(nc)}：原先{_detail(bce)}，现{_detail(nce)}")
    if not pieces:
        pieces.append("判定结果发生变化")
    return "；".join(pieces)


def diff_results(
    base_out: EvaluationOutput,
    new_out: EvaluationOutput,
    base_strategy: Strategy,
    new_strategy: Strategy,
    base_run_id: str | None = None,
    new_run_id: str | None = None,
) -> ResultDiff:
    """比较两次结果的入选集合；新增/移除逐只给出原因，changes_text 列出策略层面的变化。"""
    bs, ns = normalize(base_strategy), normalize(new_strategy)
    m = _match_conditions(bs, ns)
    changes = _changes_text(bs, ns, m)
    universe_changed = _universe_key(bs.universe) != _universe_key(ns.universe)

    base_inc = {s.thscode: s for s in base_out.included}
    new_inc = {s.thscode: s for s in new_out.included}
    base_all, new_all = _index(base_out), _index(new_out)

    added = [
        DiffItem(thscode=c, name=new_inc[c].name,
                 reason=_added_reason(base_all.get(c), new_inc[c], bs, ns, m, universe_changed))
        for c in sorted(new_inc.keys() - base_inc.keys())
    ]
    removed = [
        DiffItem(thscode=c, name=base_inc[c].name,
                 reason=_removed_reason(base_inc[c], new_all.get(c), bs, ns, m, universe_changed))
        for c in sorted(base_inc.keys() - new_inc.keys())
    ]
    if not changes:
        changes = ["策略未变化，结果差异来自数据更新（数值或数据可用性变化）"] if (added or removed) else ["策略未变化"]
    return ResultDiff(
        base_run_id=base_run_id, new_run_id=new_run_id, added=added, removed=removed,
        unchanged=len(base_inc.keys() & new_inc.keys()), changes_text=changes,
    )
