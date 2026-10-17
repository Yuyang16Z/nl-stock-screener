"""确定性冲突检测（DESIGN §5.2）。纯函数：同输入同输出，只看条件本身，不看数据。

规则：
1. 同指标、同算子族（数值之间；分位之间且同 rank_scope；布尔之间）区间求交为空 → contradiction
   （两条都是硬条件为 error，阻止执行；涉及软条件为 warning）；
2. 区间包含 → info/redundant；
3. 阈值超出 plausible_range、between 上下界颠倒 → warning/implausible；
4. 未知指标、算子不被允许、缺阈值、分位不在 [0,100]、分位口径缺失或不支持 → error/unsupported_metric；
5. 张力规则表 → warning/tension 或 warning/universe_mismatch；
6. 无任何启用的硬条件 → warning/empty_hard。
停用（enabled=False）的条件不参与任何规则。运行期的 data_coverage 由 evaluate 追加。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from itertools import combinations

from .. import catalog
from ..schemas import Condition, Conflict, Strategy, Universe, ValidateResult
from .conditions import (
    OP_LABEL,
    contains,
    fmt_num,
    interval,
    intersect,
    normalize,
    op_family,
    structural_problems,
    unit_suffix,
)

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# 张力规则阈值（与任务约定一致）
GROWTH_METRICS = ("revenue_yoy", "net_profit_yoy")
GROWTH_MIN_VALUE = 25.0  # 增速下限 ≥ 25%
GROWTH_MIN_RANK = 80.0  # 或分位 ≥ 80
VALUE_MAX_PE = 15.0  # PE 上限 ≤ 15 倍
VALUE_MAX_RANK = 20.0  # 或分位 ≤ 20
LOWVOL_MAX_VALUE = 20.0  # 波动率上限 ≤ 20%
LOWVOL_MAX_RANK = 30.0  # 或分位 ≤ 30
MOMENTUM_METRICS = ("return_20d", "return_60d")
MOMENTUM_MIN_VALUE = 15.0  # 区间涨幅下限 ≥ 15%
MOMENTUM_MIN_RANK = 80.0  # 或分位 ≥ 80
SMALL_CAP_MAX = 100.0  # 估算总市值上限 ≤ 100 亿元
LARGE_CAP_INDEXES = {"000016.SH": "上证50", "000300.SH": "沪深300"}
FINANCIAL_KEYWORDS = ("银行", "证券", "保险", "券商")
BANK_KEYWORDS = ("银行",)

_STRUCT_SUGGESTION: dict[str, str] = {
    "unknown_metric": "请从指标目录中选择指标（条件编辑器的指标下拉中可查看全部可用指标）。",
    "missing_value": "请填写数值阈值。",
    "invalid_value": "请填写有效的数值阈值。",
    "missing_value2": "“介于”需要同时填写下界与上界。",
    "invalid_value2": "请填写有效的区间上界。",
    "rank_out_of_range": "分位取值范围为 0~100（0 = 组内最小，100 = 组内最大）。",
    "missing_scope": "请选择参考范围：股票池内 / 所属行业内 / 全A股内。",
}


class _Collector:
    """收集冲突并生成稳定、唯一的 id（基于类型、规则与条件 id，而非出现顺序）。"""

    def __init__(self) -> None:
        self.items: list[Conflict] = []
        self._ids: set[str] = set()

    def add(
        self,
        severity: str,
        kind: str,
        conds: Sequence[Condition],
        message: str,
        suggestion: str | None = None,
        tag: str = "",
    ) -> None:
        ids = [c.id for c in conds]
        base = ":".join(p for p in (kind, tag, "+".join(ids)) if p)
        cid, k = base, 2
        while cid in self._ids:
            cid, k = f"{base}#{k}", k + 1
        self._ids.add(cid)
        self.items.append(
            Conflict(
                id=cid,
                severity=severity,  # type: ignore[arg-type]
                kind=kind,  # type: ignore[arg-type]
                condition_ids=ids,
                message=message,
                suggestion=suggestion,
                source="rule",
            )
        )


def _q(c: Condition) -> str:
    return f"「{c.label}」"


def _qs(conds: Sequence[Condition]) -> str:
    return "、".join(_q(c) for c in conds)


# ───────────────────────────── 规则 4：结构 ─────────────────────────────


def _check_structure(c: Condition, out: _Collector) -> bool:
    """返回条件是否结构有效。"""
    probs = structural_problems(c)
    mdef = catalog.METRICS.get(c.metric)
    for p in probs:
        suggestion = _STRUCT_SUGGESTION.get(p.code)
        if p.code == "op_not_allowed" and mdef is not None:
            ops = "、".join(OP_LABEL.get(o, o) for o in mdef.allowed_ops)
            suggestion = f"「{mdef.name}」可用的算子：{ops}。"
        elif p.code == "scope_not_allowed" and mdef is not None:
            scopes = "、".join(catalog.RANK_SCOPE_LABEL[s] for s in mdef.rank_scopes) or "无"
            suggestion = f"「{mdef.name}」可用的分位范围：{scopes}；行业/全A股参考组只对估值、行情等可全市场获取的指标提供，可改为股票池内分位。"
        out.add("error", "unsupported_metric", [c], f"条件{_q(c)}无法执行：{p.message}。", suggestion, tag=p.code)
    return not probs


# ───────────────────────────── 规则 3：阈值合理性 ─────────────────────────────


def _check_plausible(c: Condition, out: _Collector) -> None:
    mdef = catalog.METRICS[c.metric]
    if op_family(c.op) != "numeric":
        return
    if c.op == "between" and c.value is not None and c.value2 is not None and c.value > c.value2:
        out.add(
            "warning", "implausible", [c],
            f"条件{_q(c)}的下界 {fmt_num(c.value)} 大于上界 {fmt_num(c.value2)}，"
            f"已按区间 [{fmt_num(c.value2)}, {fmt_num(c.value)}] 处理。",
            "请确认区间的上下界。", tag="reversed",
        )
    if mdef.plausible_range is None:
        return
    lo, hi = mdef.plausible_range
    vals = [c.value] + ([c.value2] if c.op == "between" else [])
    bad = [float(v) for v in vals if v is not None and not lo <= float(v) <= hi]
    if not bad:
        return
    unit = mdef.unit
    shown = "、".join(f"{fmt_num(v)}{unit_suffix(unit)}" for v in bad)
    hint = f"「{mdef.name}」以“{unit}”为单位（如 20 表示 20{unit_suffix(unit)}）" if unit else f"「{mdef.name}」无单位"
    out.add(
        "warning", "implausible", [c],
        f"条件{_q(c)}的阈值 {shown} 超出该指标的合理范围 [{fmt_num(lo)}, {fmt_num(hi)}]{unit_suffix(unit)}，"
        "可能是单位或数量级有误。",
        f"请确认阈值：{hint}。", tag="range",
    )


# ───────────────────────────── 规则 1、2：区间 ─────────────────────────────


def _group_key(c: Condition) -> tuple[str, str, str | None]:
    fam = op_family(c.op) or ""
    return (c.metric, fam, c.rank_scope if fam == "rank" else None)


def _check_pair(a: Condition, b: Condition, out: _Collector) -> None:
    ia, ib = interval(a), interval(b)
    if ia is None or ib is None:
        return
    fam = op_family(a.op)
    what = "分位区间" if fam == "rank" else "取值区间"
    if intersect(ia, ib) is None:
        if a.hard and b.hard:
            out.add(
                "error", "contradiction", [a, b],
                f"{_q(a)}与{_q(b)}不可能同时满足（{what}没有交集），执行后不会有任何股票入选。",
                "删除其中一条，或调整阈值使两个区间有重叠。",
            )
        elif a.hard or b.hard:
            soft, hard = (b, a) if a.hard else (a, b)
            out.add(
                "warning", "contradiction", [a, b],
                f"软条件{_q(soft)}与硬条件{_q(hard)}互斥：入选股票都不会满足该软条件，其计分恒为 0。",
                f"删除软条件{_q(soft)}，或调整其阈值。",
            )
        else:
            out.add(
                "warning", "contradiction", [a, b],
                f"软条件{_q(a)}与{_q(b)}互斥（{what}没有交集），任何股票最多满足其中一条。",
                "删除其中一条，或调整阈值。",
            )
        return
    a_in_b, b_in_a = contains(ib, ia), contains(ia, ib)
    if a_in_b and b_in_a:
        if a.hard and b.hard:
            out.add("info", "redundant", [a, b], f"{_q(a)}与{_q(b)}等价（重复条件）。", "删除其中一条即可。")
        elif a.hard != b.hard:
            soft, hard = (b, a) if a.hard else (a, b)
            out.add(
                "info", "redundant", [a, b],
                f"软条件{_q(soft)}与硬条件{_q(hard)}等价：对入选股票恒成立，计分没有区分度。",
                f"可删除软条件{_q(soft)}。",
            )
        return
    if not (a_in_b or b_in_a):
        return
    narrow, wide = (a, b) if a_in_b else (b, a)
    if narrow.hard and wide.hard:
        out.add(
            "info", "redundant", [wide, narrow],
            f"{_q(wide)}已被更严格的{_q(narrow)}覆盖，删除前者不影响结果。",
            f"可删除{_q(wide)}以简化策略。",
        )
    elif narrow.hard and not wide.hard:
        out.add(
            "info", "redundant", [wide, narrow],
            f"软条件{_q(wide)}已被硬条件{_q(narrow)}覆盖：对入选股票恒成立，计分没有区分度。",
            f"可删除软条件{_q(wide)}，或把它改得比硬条件更严格。",
        )
    # 宽的是硬条件、窄的是软条件 / 都是软条件：属于“过滤 + 偏好”的正常分层，不提示


# ───────────────────────────── 规则 5：张力 ─────────────────────────────


def _lower(c: Condition) -> float | None:
    if c.op in ("gt", "gte") and c.value is not None:
        return float(c.value)
    if c.op == "between" and c.value is not None and c.value2 is not None:
        return float(min(c.value, c.value2))
    return None


def _upper(c: Condition) -> float | None:
    if c.op in ("lt", "lte") and c.value is not None:
        return float(c.value)
    if c.op == "between" and c.value is not None and c.value2 is not None:
        return float(max(c.value, c.value2))
    return None


def _rank_lower(c: Condition) -> float | None:
    return float(c.value) if c.op == "pct_rank_gte" and c.value is not None else None


def _rank_upper(c: Condition) -> float | None:
    return float(c.value) if c.op == "pct_rank_lte" and c.value is not None else None


def _ge(x: float | None, t: float) -> bool:
    return x is not None and x >= t


def _le(x: float | None, t: float) -> bool:
    return x is not None and x <= t


def _select(conds: Sequence[Condition], metrics: Sequence[str], pred: Callable[[Condition], bool]) -> list[Condition]:
    return [c for c in conds if c.metric in metrics and pred(c)]


def _pool_name(u: Universe) -> str:
    if u.name:
        return u.name
    if u.kind == "all_a":
        return "全部A股"
    if u.kind == "custom":
        return f"自定义（{len(u.codes)} 只）"
    return u.code or u.query or "未命名股票池"


def _pool_text(u: Universe) -> str:
    return " ".join(x for x in (u.name, u.query) if x)


def _is_large_cap_pool(u: Universe) -> bool:
    if u.kind != "index":
        return False
    return u.code in LARGE_CAP_INDEXES or any(n in (u.name or "") for n in LARGE_CAP_INDEXES.values())


def _check_tension(s: Strategy, valid: Sequence[Condition], out: _Collector) -> None:
    hard = [c for c in valid if c.hard]

    growth = _select(hard, GROWTH_METRICS,
                     lambda c: _ge(_lower(c), GROWTH_MIN_VALUE) or _ge(_rank_lower(c), GROWTH_MIN_RANK))
    value = _select(hard, ("pe_ttm",),
                    lambda c: _le(_upper(c), VALUE_MAX_PE) or _le(_rank_upper(c), VALUE_MAX_RANK))
    if growth and value:
        out.add(
            "warning", "tension", growth + value,
            f"高成长与低估值同时从严：{_qs(growth)}与{_qs(value)}都是硬条件。"
            "市场通常给予高增速公司较高的估值，两类条件叠加后入选股票可能很少。",
            "可把估值条件放宽（如“所属行业内分位 ≤ 50%”），或把其中一类改为软条件（只计分、不过滤）。",
            tag="growth_value",
        )

    lowvol = _select(hard, ("volatility_250d",),
                     lambda c: _le(_upper(c), LOWVOL_MAX_VALUE) or _le(_rank_upper(c), LOWVOL_MAX_RANK))
    momentum = _select(hard, MOMENTUM_METRICS,
                       lambda c: _ge(_lower(c), MOMENTUM_MIN_VALUE) or _ge(_rank_lower(c), MOMENTUM_MIN_RANK))
    if lowvol and momentum:
        out.add(
            "warning", "tension", lowvol + momentum,
            f"低波动与强动量同时从严：{_qs(lowvol)}与{_qs(momentum)}都是硬条件。"
            "历史数据中，短期涨幅较大的股票波动率通常也较高，两者叠加后入选股票可能很少。",
            "可放宽其中一项阈值，或把区间涨幅条件改为软条件。",
            tag="lowvol_momentum",
        )

    u = s.universe
    pool = _pool_name(u)
    small = _select(hard, ("market_cap_est",), lambda c: _le(_upper(c), SMALL_CAP_MAX))
    if small and _is_large_cap_pool(u):
        out.add(
            "warning", "universe_mismatch", small,
            f"{_qs(small)}与股票池「{pool}」不匹配：该指数成分股以大市值公司为主，"
            f"估算总市值 ≤ {fmt_num(SMALL_CAP_MAX)} 亿元的成分股可能极少甚至没有。",
            "如需小盘股，可把股票池换成中证1000 或中证500；或改用“股票池内分位 ≤ 30%”的相对口径。",
            tag="small_cap_large_index",
        )

    text = _pool_text(u)
    if any(k in text for k in FINANCIAL_KEYWORDS):
        debt = [c for c in valid if c.metric == "debt_ratio" and op_family(c.op) == "numeric"]
        if debt:
            out.add(
                "warning", "universe_mismatch", debt,
                f"股票池「{pool}」属于金融行业，资产负债率天然很高（由经营模式决定）；"
                f"{_qs(debt)}按一般企业的标准设定，可能排除几乎全部成分股或失去区分度。",
                "删除该条件，或改用“股票池内分位”与同业比较；金融股可改看 PB、ROE、股息率等指标。",
                tag="debt_financial",
            )
    if any(k in text for k in BANK_KEYWORDS):
        gm = [c for c in valid if c.metric in ("gross_margin", "gross_margin_change")]
        if gm:
            out.add(
                "warning", "universe_mismatch", gm,
                f"银行业一般不适用“销售毛利率”口径（数据源多为空值）；在股票池「{pool}」中，"
                f"{_qs(gm)}会让大量成分股因数据缺失或不适用而无法判定。",
                "删除该条件，或改用 ROE、销售净利率等指标。",
                tag="gross_margin_bank",
            )


# ───────────────────────────── 规则 6 与权重 ─────────────────────────────


def _check_weights(valid: Sequence[Condition], out: _Collector) -> None:
    for c in valid:
        if c.hard:
            continue
        w = float(c.weight)
        if not math.isfinite(w) or w < 0:
            out.add("error", "implausible", [c], f"软条件{_q(c)}的权重 {fmt_num(w)} 无效（必须为非负数）。",
                    "请把权重设为正数（默认 1）。", tag="weight")
        elif w == 0:
            out.add("info", "implausible", [c], f"软条件{_q(c)}的权重为 0，不影响排序。",
                    "如需参与排序，请把权重设为正数。", tag="weight")


def detect_conflicts(strategy: Strategy) -> list[Conflict]:
    """纯确定性冲突检测；结果按严重程度（error → warning → info）稳定排序。"""
    s = normalize(strategy)
    active = [c for c in s.conditions if c.enabled]
    out = _Collector()

    valid = [c for c in active if _check_structure(c, out)]

    groups: dict[tuple[str, str, str | None], list[Condition]] = {}
    for c in valid:
        groups.setdefault(_group_key(c), []).append(c)
    for members in groups.values():
        for a, b in combinations(members, 2):
            _check_pair(a, b, out)

    for c in valid:
        _check_plausible(c, out)

    _check_tension(s, valid, out)
    _check_weights(valid, out)
    _check_negative_values(valid, out)
    _check_report_period(s, out)

    if not any(c.hard for c in active):
        out.add(
            "warning", "empty_hard", [c for c in active],
            "当前没有启用的硬条件：股票池内全部股票都会入选，软条件只影响排序。",
            "至少保留一条硬条件（如默认风控条件）来过滤股票。",
        )

    return sorted(out.items, key=lambda x: _SEVERITY_ORDER[x.severity])


def _lower_bound_positive(c: Condition) -> bool:
    """该条件是否保证数值 > 0（或 ≥ 0），从而排除负值。"""
    if c.op in ("gt", "gte") and c.value is not None:
        return c.value >= 0
    if c.op == "between" and c.value is not None and c.value2 is not None:
        return min(c.value, c.value2) >= 0
    return False


def _check_negative_values(valid: Sequence[Condition], out: _Collector) -> None:
    """估值倍数为负（亏损、净资产或现金流为负）时，“PE < 30”这类上限条件也会成立。"""
    by_metric: dict[str, list[Condition]] = {}
    for c in valid:
        md = catalog.METRICS.get(c.metric)
        if md is not None and md.positive_only_for_rank and c.op in ("lt", "lte"):
            by_metric.setdefault(c.metric, []).append(c)
    for metric, caps in by_metric.items():
        guarded = any(_lower_bound_positive(c) for c in valid if c.metric == metric)
        if guarded:
            continue
        name = catalog.METRICS[metric].name
        out.add(
            "warning", "implausible", caps,
            f"{_qs(caps)}对{name}为负的公司（如亏损公司）同样成立：负值会被判为“通过”。",
            f"如需排除亏损/负值，补充条件「{name} > 0」，或改为“介于 0 ~ 上限”。",
            tag=f"negative_{metric}",
        )


def _check_report_period(s: Strategy, out: _Collector) -> None:
    if not s.report_period:
        return
    from ..data.periods import parse_report, report_deadline, report_label, today_cn

    try:
        y, q = parse_report(s.report_period)
    except ValueError as e:
        out.add("error", "implausible", [], f"报告期“{s.report_period}”无效：{e}", "改为 yyyy-1 ~ yyyy-4，或选“自动”。", tag="report_period")
        return
    rep = f"{y}-{q}"
    deadline = report_deadline(rep)
    if today_cn() <= deadline:
        out.add(
            "warning", "data_coverage", [],
            f"指定的报告期 {report_label(rep)} 法定披露截止日为 {deadline.isoformat()}，尚未到期：多数公司可能还没有披露，相关财务条件会大面积显示为“缺失”。",
            "如需可比的全市场数据，选“自动”（取法定截止日已过的最近一期）。",
            tag="report_period_future",
        )


def _strategy_warnings(s: Strategy) -> list[str]:
    warnings: list[str] = []
    u = s.universe
    if u.kind == "custom" and not u.codes:
        warnings.append("自定义股票池为空，执行后不会有任何股票。")
    if u.kind in ("index", "industry", "concept") and not u.code:
        what = f"「{u.query}」" if u.query else ""
        warnings.append(f"股票池{what}尚未解析为具体指数/板块代码，请先选择股票池。")
    if s.sort_metric and s.sort_metric not in catalog.METRICS:
        warnings.append(f"排序指标「{s.sort_metric}」未登记，排序时将忽略。")
    disabled = [c for c in s.conditions if not c.enabled]
    if disabled:
        warnings.append(f"有 {len(disabled)} 条条件已停用，不参与执行与冲突检测。")
    return warnings


def validate_strategy(strategy: Strategy) -> ValidateResult:
    """normalize + detect_conflicts；can_run = 无 error 级冲突。"""
    s = normalize(strategy)
    conflicts = detect_conflicts(s)
    return ValidateResult(
        strategy=s,
        conflicts=conflicts,
        can_run=not any(c.severity == "error" for c in conflicts),
        warnings=_strategy_warnings(s),
    )
