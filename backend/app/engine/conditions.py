"""条件的可读描述、阈值文本、区间工具、规范化与澄清应用（DESIGN §5.1）。

全部为纯函数：不读写外部状态，不修改入参（需要修改时先深拷贝）。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal, NamedTuple

from .. import catalog
from ..schemas import Clarification, Condition, MetricDef, Strategy
from .ops import is_finite_number

OpFamily = Literal["numeric", "rank", "bool"]
# (下界, 是否含下界, 上界, 是否含上界)；无界用 ±inf
Interval = tuple[float, bool, float, bool]

NUMERIC_OPS: frozenset[str] = frozenset(catalog.NUMERIC_OPS)
RANK_OPS: frozenset[str] = frozenset(catalog.RANK_OPS)
BOOL_OPS: frozenset[str] = frozenset(catalog.BOOL_OPS)

_CMP_SYMBOL: dict[str, str] = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
_RANK_SYMBOL: dict[str, str] = {"pct_rank_lte": "≤", "pct_rank_gte": "≥"}
# 阈值文本用的短口径名（describe 用 catalog.RANK_SCOPE_LABEL 的完整说法）
SCOPE_SHORT: dict[str, str] = {"universe": "股票池内", "industry": "行业内", "market": "全A股内"}
OP_LABEL: dict[str, str] = {o.op: o.label for o in catalog.OPS}

# 系统默认风控条件（ST、次新），来源于 catalog；对外给深拷贝，避免被就地修改
DEFAULT_SYSTEM_CONDITIONS: tuple[Condition, ...] = tuple(c.model_copy(deep=True) for c in catalog.DEFAULT_CONDITIONS)


def default_system_conditions() -> list[Condition]:
    """返回默认风控条件的新副本（origin="system"，可删除）。"""
    return [c.model_copy(deep=True) for c in catalog.DEFAULT_CONDITIONS]


# ───────────────────────────── 数字与单位格式 ─────────────────────────────


def fmt_num(v: float | int | None, digits: int = 4) -> str:
    """去掉多余小数位的数字文本：25.0→"25"，1.50→"1.5"，-0→"0"。"""
    if v is None:
        return "?"
    x = float(v)
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "+∞" if x > 0 else "-∞"
    s = f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def unit_suffix(unit: str) -> str:
    """单位后缀：% 紧贴数字，其余单位前加空格，无单位为空。"""
    if not unit:
        return ""
    return unit if unit == "%" else f" {unit}"


def with_unit(v: float | int | None, unit: str) -> str:
    return f"{fmt_num(v)}{unit_suffix(unit)}"


def fmt_value(value: float | bool | None, unit: str) -> str:
    """无 Evidence.display 时的兜底展示值。"""
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "是" if value else "否"
    x = float(value)
    if not math.isfinite(x):
        return "--"
    return with_unit(round(x, 2), unit)


# ───────────────────────────── 算子族与描述 ─────────────────────────────


def op_family(op: str) -> OpFamily | None:
    if op in NUMERIC_OPS:
        return "numeric"
    if op in RANK_OPS:
        return "rank"
    if op in BOOL_OPS:
        return "bool"
    return None


def _metric_name_unit(cond: Condition, metric_def: MetricDef | None) -> tuple[str, str]:
    mdef = metric_def or catalog.METRICS.get(cond.metric)
    if mdef is None:
        return cond.metric, ""
    return mdef.name, mdef.unit


def _sorted_bounds(cond: Condition) -> tuple[float | None, float | None]:
    lo, hi = cond.value, cond.value2
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return lo, hi


def describe(cond: Condition, metric_def: MetricDef | None = None) -> str:
    """条件的可读描述，如“市盈率TTM 在所属行业内分位 ≤ 50%”“是否ST：否”。

    between 按“小~大”展示（与 ops.check_op 自动交换上下界的语义一致）。
    """
    name, unit = _metric_name_unit(cond, metric_def)
    op = cond.op
    if op in _CMP_SYMBOL:
        return f"{name} {_CMP_SYMBOL[op]} {with_unit(cond.value, unit)}"
    if op == "between":
        lo, hi = _sorted_bounds(cond)
        return f"{name} 介于 {fmt_num(lo)}~{with_unit(hi, unit)}"
    if op in _RANK_SYMBOL:
        scope = catalog.RANK_SCOPE_LABEL.get(cond.rank_scope or "", "参考组内")
        return f"{name} 在{scope}分位 {_RANK_SYMBOL[op]} {fmt_num(cond.value)}%"
    if op == "is_true":
        return f"{name}：是"
    if op == "is_false":
        return f"{name}：否"
    return f"{name} {op} {fmt_num(cond.value)}"


def threshold_text(cond: Condition, metric_def: MetricDef | None = None) -> str:
    """阈值文本，如“> 0%”“行业内分位 ≤ 50%”“∈ [0, 25] 倍”。"""
    _, unit = _metric_name_unit(cond, metric_def)
    op = cond.op
    if op in _CMP_SYMBOL:
        return f"{_CMP_SYMBOL[op]} {with_unit(cond.value, unit)}"
    if op == "between":
        lo, hi = _sorted_bounds(cond)
        return f"∈ [{fmt_num(lo)}, {fmt_num(hi)}]{unit_suffix(unit)}"
    if op in _RANK_SYMBOL:
        scope = SCOPE_SHORT.get(cond.rank_scope or "", "参考组内")
        return f"{scope}分位 {_RANK_SYMBOL[op]} {fmt_num(cond.value)}%"
    if op == "is_true":
        return "是"
    if op == "is_false":
        return "否"
    return f"{op} {fmt_num(cond.value)}"


def semantic_key(cond: Condition) -> tuple[object, ...]:
    """只含影响判定的字段（指标、算子、阈值、分位口径），用于判断两条条件是否同义。"""
    fam = op_family(cond.op)
    if fam == "bool":
        return (cond.metric, cond.op, None, None, None)
    if cond.op == "between":
        lo, hi = _sorted_bounds(cond)
        return (cond.metric, cond.op, lo, hi, None)
    if fam == "rank":
        return (cond.metric, cond.op, cond.value, None, cond.rank_scope)
    return (cond.metric, cond.op, cond.value, None, None)


# ───────────────────────────── 区间工具 ─────────────────────────────


def interval(cond: Condition) -> Interval | None:
    """条件的取值区间。数值算子作用于指标值；分位算子作用于 [0,100] 分位；布尔：是=[1,1]、否=[0,0]。

    阈值缺失或非有限数时返回 None。
    """
    fam = op_family(cond.op)
    if fam == "bool":
        x = 1.0 if cond.op == "is_true" else 0.0
        return (x, True, x, True)
    if fam is None or not is_finite_number(cond.value):
        return None
    v = float(cond.value)  # type: ignore[arg-type]
    inf = math.inf
    op = cond.op
    if op == "gt":
        return (v, False, inf, False)
    if op == "gte":
        return (v, True, inf, False)
    if op == "lt":
        return (-inf, False, v, False)
    if op == "lte":
        return (-inf, False, v, True)
    if op == "between":
        if not is_finite_number(cond.value2):
            return None
        lo, hi = sorted((v, float(cond.value2)))  # type: ignore[arg-type]
        return (lo, True, hi, True)
    if op == "pct_rank_lte":
        return (0.0, True, v, True)
    if op == "pct_rank_gte":
        return (v, True, 100.0, True)
    return None


def intersect(a: Interval, b: Interval) -> Interval | None:
    """两个区间的交集；为空返回 None。"""
    if a[0] > b[0]:
        lo, lo_incl = a[0], a[1]
    elif b[0] > a[0]:
        lo, lo_incl = b[0], b[1]
    else:
        lo, lo_incl = a[0], a[1] and b[1]
    if a[2] < b[2]:
        hi, hi_incl = a[2], a[3]
    elif b[2] < a[2]:
        hi, hi_incl = b[2], b[3]
    else:
        hi, hi_incl = a[2], a[3] and b[3]
    if lo > hi or (lo == hi and not (lo_incl and hi_incl)):
        return None
    return (lo, lo_incl, hi, hi_incl)


def contains(outer: Interval, inner: Interval) -> bool:
    """outer ⊇ inner。"""
    lo_ok = outer[0] < inner[0] or (outer[0] == inner[0] and (outer[1] or not inner[1]))
    hi_ok = outer[2] > inner[2] or (outer[2] == inner[2] and (outer[3] or not inner[3]))
    return lo_ok and hi_ok


# ───────────────────────────── 结构校验 ─────────────────────────────


class Problem(NamedTuple):
    code: str  # unknown_metric / op_not_allowed / missing_value / invalid_value / missing_value2 / invalid_value2 / rank_out_of_range / missing_scope / scope_not_allowed
    message: str


def structural_problems(cond: Condition) -> list[Problem]:
    """条件无法执行的结构性问题（未知指标、非法算子、缺阈值、分位越界、口径不支持）。"""
    mdef = catalog.METRICS.get(cond.metric)
    if mdef is None:
        return [Problem("unknown_metric", f"未登记的指标「{cond.metric}」")]
    if cond.op not in mdef.allowed_ops:
        return [Problem("op_not_allowed", f"指标「{mdef.name}」不支持算子「{OP_LABEL.get(cond.op, cond.op)}」")]
    fam = op_family(cond.op)
    probs: list[Problem] = []
    if fam in ("numeric", "rank"):
        if cond.value is None:
            probs.append(Problem("missing_value", "缺少阈值"))
        elif not is_finite_number(cond.value):
            probs.append(Problem("invalid_value", "阈值不是有效数字"))
    if cond.op == "between":
        if cond.value2 is None:
            probs.append(Problem("missing_value2", "“介于”缺少区间上界"))
        elif not is_finite_number(cond.value2):
            probs.append(Problem("invalid_value2", "区间上界不是有效数字"))
    if fam == "rank":
        if is_finite_number(cond.value) and not 0 <= float(cond.value) <= 100:  # type: ignore[arg-type]
            probs.append(Problem("rank_out_of_range", f"分位阈值 {fmt_num(cond.value)} 不在 0~100 之间"))
        if cond.rank_scope is None:
            probs.append(Problem("missing_scope", "分位条件未指定参考范围"))
        elif cond.rank_scope not in mdef.rank_scopes:
            scope = catalog.RANK_SCOPE_LABEL.get(cond.rank_scope, cond.rank_scope)
            probs.append(Problem("scope_not_allowed", f"「{mdef.name}」不支持{scope}分位"))
    return probs


# ───────────────────────────── 规范化与哈希 ─────────────────────────────


def normalize(strategy: Strategy) -> Strategy:
    """补 id（空或重复的补 c1, c2…，跳过已被占用的 id）、label=describe；不改条件顺序、不删条件。"""
    s = strategy.model_copy(deep=True)
    reserved: set[str] = set()
    keep: list[bool] = []
    for c in s.conditions:
        cid = (c.id or "").strip()
        if cid and cid not in reserved:
            reserved.add(cid)
            keep.append(True)
        else:
            keep.append(False)
    counter = 0
    for c, kept in zip(s.conditions, keep):
        if kept:
            c.id = c.id.strip()
        else:
            counter += 1
            while f"c{counter}" in reserved:
                counter += 1
            c.id = f"c{counter}"
            reserved.add(c.id)
        c.label = describe(c)
    return s


def strategy_hash(strategy: Strategy) -> str:
    """规范化后的 JSON（键排序、忽略 name 与各条件 label）的 sha256。"""
    ns = normalize(strategy)
    data = ns.model_dump(mode="json", exclude={"name": True, "conditions": {"__all__": {"label"}}})
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ───────────────────────────── 澄清 ─────────────────────────────


def apply_clarification(strategy: Strategy, clarification: Clarification, option_id: str) -> Strategy:
    """应用澄清选项：删除该短语下 origin 为 ai/rule 的条件，在原位置插入所选项条件；股票池类选项替换 universe。

    插入的条件统一挂到该短语下（source_phrase=phrase，origin 取被替换条件的来源，缺省 "rule"），
    因此可以对同一澄清反复切换选项。与剩余条件 id 冲突的选项条件会重新编号，已有条件 id 保持不变。
    """
    option = next((o for o in clarification.options if o.id == option_id), None)
    if option is None:
        raise ValueError(f"澄清问题「{clarification.question}」没有选项 {option_id!r}")
    s = strategy.model_copy(deep=True)
    phrase = clarification.phrase
    kept: list[Condition] = []
    insert_at: int | None = None
    removed_origin: str | None = None
    for c in s.conditions:
        if c.source_phrase == phrase and c.origin in ("ai", "rule"):
            if insert_at is None:
                insert_at = len(kept)
                removed_origin = c.origin
            continue
        kept.append(c)
    used = {c.id for c in kept if c.id}
    inserted: list[Condition] = []
    for oc in option.conditions:
        nc = oc.model_copy(deep=True)
        nc.source_phrase = phrase
        if nc.origin not in ("ai", "rule"):
            nc.origin = removed_origin or "rule"  # type: ignore[assignment]
        if not nc.id or nc.id in used:
            nc.id = ""
        else:
            used.add(nc.id)
        nc.label = None
        inserted.append(nc)
    pos = insert_at if insert_at is not None else len(kept)
    s.conditions = kept[:pos] + inserted + kept[pos:]
    if option.universe is not None:
        s.universe = option.universe.model_copy(deep=True)
    return normalize(s)
