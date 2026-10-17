"""确定性判定：逐股逐条件状态、分桶、软分排序、漏斗、条件统计（DESIGN §5.3）。

判定语义：
- 指标键不存在：快照为预筛模式（prefiltered）→ skipped“已被前置条件排除，未取数”；否则 missing“未获取该指标”；
- 指标 status=missing/error → missing（error 的原因带错误信息）；not_applicable → not_applicable；
- 指标 ok 但值为空/非有限数/类型不符 → missing；stale（过期）→ missing（过期数据不判为通过）；
- 其余按算子判定 pass/fail；数值判定、分位、margin 一律调用 engine/ops.py。
缺失、失败、不适用、跳过的条件永远不会被当作“通过”。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable

import numpy as np

from .. import catalog
from ..schemas import (
    Condition,
    ConditionStat,
    CondEval,
    Conflict,
    EvaluationOutput,
    Evidence,
    FeatureSnapshot,
    FunnelStep,
    MetricDef,
    MetricValue,
    StockResult,
    Strategy,
)
from .conditions import fmt_value, normalize, op_family, strategy_hash, structural_problems, threshold_text
from .ops import MIN_RANK_GROUP, is_finite_number, margin, pct_rank, rank_margin
from .sensitivity import (
    FAIL,
    MISSING,
    NA,
    PASS,
    SKIPPED,
    STATUS_NAMES,
    CondColumn,
    EvalMatrix,
    drop_count,
    judge,
    near_miss_mask,
    sole_blocker_count,
    sweep_points,
    sweep_values_for,
)

FUNNEL_NOTE = "漏斗按条件顺序逐步过滤，用于观察各条件的边际影响；条件顺序不影响最终入选结果。"
COVERAGE_WARN_RATIO = 0.2  # 缺失 + 不适用 占比超过 20% → data_coverage

REASON_SKIPPED = "已被前置条件排除，未取数"
REASON_NOT_FETCHED = "未获取该指标"
REASON_NO_REFERENCE = "参考组数据不可用"
REASON_NO_INDUSTRY = "行业归属未知"

# positive_only_for_rank 指标非正值时的说明
_NONPOS_SHORT = {"pe_ttm": "PE", "pb_mrq": "PB", "ps_ttm": "PS", "pcf_ttm": "PCF"}
_NONPOS_HINT = {"pe_ttm": "亏损", "pb_mrq": "净资产为负", "ps_ttm": "营业收入为负", "pcf_ttm": "经营现金流为负"}


class InvalidStrategyError(ValueError):
    """启用的条件存在结构性问题（未知指标、非法算子、缺阈值等），无法执行。应先调用 validate_strategy。"""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("策略包含无法执行的条件：" + "；".join(problems))


# ───────────────────────────── 单列构建 ─────────────────────────────


def _status_reason(mv: MetricValue) -> str:
    if mv.status == "error":
        if mv.reason and "失败" in mv.reason:
            return mv.reason
        return f"数据获取失败：{mv.reason}" if mv.reason else "数据获取失败"
    if mv.status == "not_applicable":
        return mv.reason or "该指标对该股不适用"
    return mv.reason or "数据缺失（接口未返回该值）"


def _value_problem(raw: object, family: str) -> str | None:
    """ok 状态下的值是否可用于判定；不可用时返回原因。"""
    if raw is None:
        return "数值为空"
    if family == "bool":
        if isinstance(raw, bool) or (is_finite_number(raw) and float(raw) in (0.0, 1.0)):  # type: ignore[arg-type]
            return None
        return "数值类型不符（应为是/否）"
    if isinstance(raw, bool):
        return "数值类型不符（应为数值）"
    if not is_finite_number(raw):
        return "数值无效（非有限数）"
    return None


def _nonpositive_reason(mdef: MetricDef, v: float) -> str:
    short = _NONPOS_SHORT.get(mdef.id, mdef.name)
    if v < 0:
        hint = _NONPOS_HINT.get(mdef.id)
        return f"{short} 为负（{hint}），分位不适用" if hint else f"{short} 为负，分位不适用"
    return f"{short} 为 0，分位不适用"


def _build_column(cond: Condition, codes: list[str], snap: FeatureSnapshot) -> CondColumn:
    mdef = catalog.METRICS[cond.metric]
    fam = op_family(cond.op) or "numeric"
    n = len(codes)
    status = np.full(n, PASS, dtype=np.int8)
    judge_vals: list[float | bool | None] = [None] * n
    values: list[float | bool | None] = [None] * n
    displays: list[str] = ["--"] * n
    reasons: list[str | None] = [None] * n
    margins: list[float | None] = [None] * n
    ranks: list[float | None] = [None] * n
    sizes: list[int | None] = [None] * n
    evidences: list[Evidence | None] = [None] * n
    usable: list[int] = []

    for i, code in enumerate(codes):
        mv = snap.stocks[code].metrics.get(cond.metric)
        if mv is None:
            status[i] = SKIPPED if snap.prefiltered else MISSING
            reasons[i] = REASON_SKIPPED if snap.prefiltered else REASON_NOT_FETCHED
            continue
        raw = mv.value
        if isinstance(raw, bool) or is_finite_number(raw):
            values[i] = raw
        evidences[i] = mv.evidence
        displays[i] = mv.evidence.display if mv.evidence is not None else fmt_value(values[i], mdef.unit)
        if mv.status != "ok":
            status[i] = NA if mv.status == "not_applicable" else MISSING
            reasons[i] = _status_reason(mv)
            continue
        problem = _value_problem(raw, fam)
        if problem is not None:
            status[i] = MISSING
            reasons[i] = problem
            continue
        if mv.stale:  # 过期数据不判为通过（DESIGN §1/§9）；features 把原因写在 evidence.note
            status[i] = MISSING
            why = mv.reason or (mv.evidence.note if mv.evidence is not None else None) or "早于预期时点"
            reasons[i] = f"数据时点过期（{why}），不参与判定"
            continue
        usable.append(i)

    col = CondColumn(
        cond=cond, mdef=mdef, family=fam, threshold=threshold_text(cond, mdef), status=status,
        judge_vals=judge_vals, values=values, displays=displays, reasons=reasons, margins=margins,
        ranks=ranks, group_sizes=sizes, evidences=evidences,
    )
    if fam == "rank":
        _judge_rank(col, codes, snap, usable)
    else:
        for i in usable:
            x = values[i]
            assert x is not None
            if fam == "bool":
                x = bool(x)
            judge_vals[i] = x
            status[i] = PASS if judge(cond, x) else FAIL
            if fam == "numeric":
                mg = margin(cond.op, float(x), cond.value, cond.value2)
                margins[i] = None if mg is None else round(mg, 6)
    return col


def _judge_rank(col: CondColumn, codes: list[str], snap: FeatureSnapshot, usable: list[int]) -> None:
    """分位判定：平均秩法（ops.pct_rank），参考组见模块说明；判定用 ops.check_op。"""
    cond, mdef = col.cond, col.mdef
    scope = cond.rank_scope
    pos_only = mdef.positive_only_for_rank

    def keep(v: float) -> bool:
        return v > 0 if pos_only else True

    own: dict[str, float] = {codes[i]: float(col.values[i]) for i in usable}  # type: ignore[arg-type]
    group_of: Callable[[str], list[float] | None]
    if scope == "universe":
        universe_group = sorted(v for v in own.values() if keep(v))
        group_of = lambda _code: universe_group  # noqa: E731
        ref_ok = True
        if snap.prefiltered and int((col.status == SKIPPED).sum()) > 0:
            col.notes.append(f"预筛模式：股票池内分位的参考组只含已取数的 {len(universe_group)} 只股票")
    else:
        ref = snap.reference.get(cond.metric)
        ref_ok = bool(ref)
        merged: dict[str, float] = {}
        if ref_ok:
            merged = {c: float(v) for c, v in ref.items() if is_finite_number(v)}  # type: ignore[union-attr]
            merged.update(own)  # 本股以自身取数为准，并保证本股在参考组内
        if scope == "market":
            market_group = sorted(v for v in merged.values() if keep(v))
            group_of = lambda _code: market_group  # noqa: E731
        else:
            ind_of = dict(snap.reference_industry)
            for code in codes:
                ic = snap.stocks[code].info.industry_code
                if ic:
                    ind_of[code] = ic
            groups: dict[str, list[float]] = defaultdict(list)
            for c, v in merged.items():
                ic = ind_of.get(c)
                if ic and keep(v):
                    groups[ic].append(v)
            for g in groups.values():
                g.sort()

            def group_of(code: str) -> list[float] | None:
                ic = snap.stocks[code].info.industry_code
                return groups.get(ic, []) if ic else None

    threshold = float(cond.value)  # type: ignore[arg-type]
    for i in usable:
        code = codes[i]
        v = float(col.values[i])  # type: ignore[arg-type]
        if not keep(v):
            col.status[i] = NA
            col.reasons[i] = _nonpositive_reason(mdef, v)
            continue
        if not ref_ok:
            col.status[i] = MISSING
            col.reasons[i] = REASON_NO_REFERENCE
            continue
        group = group_of(code)
        if group is None:
            col.status[i] = NA
            col.reasons[i] = REASON_NO_INDUSTRY
            continue
        size = len(group)
        col.group_sizes[i] = size
        if size < MIN_RANK_GROUP:
            col.status[i] = NA
            col.reasons[i] = f"参考组样本不足（n={size}）"
            continue
        r = round(pct_rank(group, v), 4)
        col.ranks[i] = r
        col.judge_vals[i] = r
        col.status[i] = PASS if judge(cond, r) else FAIL
        col.margins[i] = round(rank_margin(cond.op, r, threshold), 6)


# ───────────────────────────── 矩阵 ─────────────────────────────


def build_matrix(strategy: Strategy, snap: FeatureSnapshot, *, conditions: list[Condition] | None = None) -> EvalMatrix:
    """规范化策略并算出“股票 × 启用条件”状态矩阵（conditions 指定时只算这些条件，须已规范化）。

    启用的条件存在结构性问题时抛 InvalidStrategyError（矛盾等逻辑冲突不阻止计算）。
    """
    ns = normalize(strategy)
    conds = [c for c in ns.conditions if c.enabled] if conditions is None else list(conditions)
    problems = [f"{c.id}（{c.label}）：{p.message}" for c in conds for p in structural_problems(c)]
    if problems:
        raise InvalidStrategyError(problems)
    codes = sorted(snap.stocks)
    columns = [_build_column(c, codes, snap) for c in conds]
    return EvalMatrix(strategy=ns, codes=codes, columns=columns, keep_flagged=ns.missing_policy == "keep_flagged")


def prefilter(strategy: Strategy, snap: FeatureSnapshot, cheap_metrics: set[str]) -> list[str]:
    """all_a 两段式评估的第一段：只用“廉价指标”的启用硬条件判定，返回值得深度取数的股票（按代码排序）。

    与最终分桶一致：廉价硬条件无 fail；missing_policy=exclude 时还要求廉价指标不缺失
    （缺失的股票无论深度数据如何都进“数据缺失”组）；keep_flagged 时缺失的股票仍需深度评估。
    """
    ns = normalize(strategy)
    conds = [c for c in ns.conditions if c.enabled and c.hard and c.metric in cheap_metrics]
    m = build_matrix(ns, snap, conditions=conds)
    mask = m.included_mask()
    return [code for code, ok in zip(m.codes, mask.tolist()) if ok]


# ───────────────────────────── 汇总 ─────────────────────────────


def _soft_scores(m: EvalMatrix) -> list[float | None]:
    soft = [(col, max(0.0, float(col.cond.weight)) if math.isfinite(col.cond.weight) else 0.0)
            for col in m.columns if not col.cond.hard]
    total = sum(w for _, w in soft)
    if not soft or total <= 0:
        return [None] * len(m.codes)
    acc = np.zeros(len(m.codes), dtype=np.float64)
    for col, w in soft:
        acc += w * (col.status == PASS)  # 缺失/不适用/跳过按未满足计 0 分
    return [round(float(x) / total, 6) for x in acc]


def _sort_value(snap: FeatureSnapshot, code: str, metric: str | None) -> float | None:
    if not metric:
        return None
    mv = snap.stocks[code].metrics.get(metric)
    if mv is None or mv.status != "ok" or not is_finite_number(mv.value):
        return None
    return float(mv.value)  # type: ignore[arg-type]


def _funnel(m: EvalMatrix, snap: FeatureSnapshot) -> list[FunnelStep]:
    n = len(m.codes)
    steps = [FunnelStep(condition_id=None, label=f"股票池：{snap.universe.name}", remaining=n)]
    order = list(m.hard_idx)
    if snap.prefiltered:  # 预筛模式按实际评估顺序：先全量取数的条件，再深度条件
        order.sort(key=lambda j: (bool((m.columns[j].status == SKIPPED).any()), j))
    alive = np.ones(n, dtype=bool)
    for j in order:
        col = m.columns[j]
        st = col.status
        fail = alive & (st == FAIL)
        blocking = st == SKIPPED  # 跳过永远阻止入选；缺失/不适用只在 exclude 策略下阻止
        if not m.keep_flagged:
            blocking = blocking | (st == MISSING) | (st == NA)
        miss = alive & blocking
        alive &= ~(fail | miss)
        steps.append(FunnelStep(
            condition_id=col.cond.id, label=col.cond.label or col.cond.id, remaining=int(alive.sum()),
            removed_fail=int(fail.sum()), removed_missing=int(miss.sum()),
        ))
    return steps


def _condition_stats(m: EvalMatrix, compute_stats: bool) -> list[ConditionStat]:
    out: list[ConditionStat] = []
    n_inc = m.included_count()
    for j, col in enumerate(m.columns):
        counts = np.bincount(col.status, minlength=5).tolist()
        notes = list(col.notes)
        stat = ConditionStat(
            condition_id=col.cond.id, label=col.cond.label or col.cond.id, hard=col.cond.hard,
            passed=counts[PASS], failed=counts[FAIL], missing=counts[MISSING],
            not_applicable=counts[NA], skipped=counts[SKIPPED],
        )
        if col.cond.hard:
            stat.sole_blocker = sole_blocker_count(m, j)
            if compute_stats:
                stat.drop_effect = drop_count(m, j)
                if stat.drop_effect is None:
                    notes.append("预筛模式：去掉该条件后，原先被它排除的股票未获取其余指标，无法精确重算入选数")
                values = sweep_values_for(col.cond, col.mdef)
                if values:
                    stat.sweep, lower = sweep_points(m, j, values)
                    if lower:
                        notes.append("部分扫描点为下限：放宽阈值后新进入候选的股票在预筛阶段未取数")
        else:
            if compute_stats:
                stat.drop_effect = n_inc
            notes.append("软条件：只参与计分排序，不影响入选数")
        stat.note = "；".join(notes) if notes else None
        out.append(stat)
    return out


def _coverage_conflicts(m: EvalMatrix) -> list[Conflict]:
    out: list[Conflict] = []
    for col in m.columns:
        st = col.status
        skipped = int((st == SKIPPED).sum())
        evaluated = len(m.codes) - skipped
        bad_mask = (st == MISSING) | (st == NA)
        bad = int(bad_mask.sum())
        if evaluated <= 0 or bad / evaluated <= COVERAGE_WARN_RATIO:
            continue
        top_reason, top_n = Counter(col.reasons[i] or "原因未知" for i in np.nonzero(bad_mask)[0].tolist()).most_common(1)[0]
        pct = round(100.0 * bad / evaluated)
        effect = "这些股票无法确认是否满足该条件。" if col.cond.hard else "软条件计分时按未满足处理。"
        out.append(Conflict(
            id=f"data_coverage:{col.cond.id}", severity="warning", kind="data_coverage",
            condition_ids=[col.cond.id],
            message=(f"条件「{col.cond.label}」有 {bad}/{evaluated} 只股票数据缺失或不适用（{pct}%），{effect}"
                     f"主要原因：{top_reason}（{top_n} 只）。"),
            suggestion="可在“数据缺失”分组查看逐股原因；可把缺失策略改为“保留并标记”，或改用覆盖更全的指标/口径。",
            source="rule",
        ))
    return out


def _cond_eval(col: CondColumn, i: int) -> CondEval:
    return CondEval(
        condition_id=col.cond.id, status=STATUS_NAMES[int(col.status[i])],  # type: ignore[arg-type]
        value=col.values[i], display=col.displays[i], threshold=col.threshold,
        rank_pct=col.ranks[i], rank_group_size=col.group_sizes[i], margin=col.margins[i],
        reason=col.reasons[i], evidence=col.evidences[i],
    )


def evaluate(strategy: Strategy, snap: FeatureSnapshot, *, compute_stats: bool = True) -> EvaluationOutput:
    """逐股逐条件判定 + 分桶 + 排序 + 漏斗 + 条件统计。同输入同输出。

    compute_stats=False 时跳过 drop_effect 与阈值扫描（计数、单独卡点、漏斗、差一点入选仍计算）。
    返回的 conflicts 只含运行期冲突（data_coverage）；静态冲突请用 conflicts.detect_conflicts。
    """
    m = build_matrix(strategy, snap)
    ns = m.strategy
    hard_cols = [m.columns[j] for j in m.hard_idx]
    soft_scores = _soft_scores(m)
    near = near_miss_mask(m).tolist()

    included: list[StockResult] = []
    excluded: list[StockResult] = []
    missing: list[StockResult] = []
    for i, code in enumerate(m.codes):
        info = snap.stocks[code].info
        failed = [col.cond.id for col in hard_cols if col.status[i] == FAIL]
        skipped = any(col.status[i] == SKIPPED for col in hard_cols)
        # 已有 fail 的股票，“未取数”不是它的缺失原因，不列入 missing_conditions
        unresolved_codes = (MISSING, NA) if failed else (MISSING, NA, SKIPPED)
        unresolved = [col.cond.id for col in hard_cols if col.status[i] in unresolved_codes]
        if failed:
            bucket = "excluded"
        elif skipped or (unresolved and not m.keep_flagged):
            bucket = "missing"
        else:
            bucket = "included"
        sr = StockResult(
            thscode=code, name=info.name, industry=info.industry, bucket=bucket,  # type: ignore[arg-type]
            failed_conditions=failed, missing_conditions=unresolved, soft_score=soft_scores[i],
            evaluations=[_cond_eval(col, i) for col in m.columns],
            near_miss=bool(near[i]), flagged=bucket == "included" and bool(unresolved),
        )
        {"included": included, "excluded": excluded, "missing": missing}[bucket].append(sr)

    sort_metric = ns.sort_metric if ns.sort_metric in catalog.METRICS else None
    sign = -1.0 if ns.sort_desc else 1.0

    def inc_key(sr: StockResult) -> tuple[float, int, float, str]:
        v = _sort_value(snap, sr.thscode, sort_metric)
        return (-(sr.soft_score or 0.0), 0 if v is not None else 1, sign * v if v is not None else 0.0, sr.thscode)

    included.sort(key=inc_key)  # excluded / missing 已按代码顺序生成

    return EvaluationOutput(
        included=included, excluded=excluded, missing=missing,
        funnel=_funnel(m, snap), condition_stats=_condition_stats(m, compute_stats),
        conflicts=_coverage_conflicts(m), strategy_hash=strategy_hash(ns),
    )
