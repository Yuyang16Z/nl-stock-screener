"""算子与分位的唯一实现：筛选引擎与回测共用，保证同一条件在两处语义一致。"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Sequence

MIN_RANK_GROUP = 5  # 分位参考组最小样本数


def check_op(op: str, value: float | bool | None, v1: float | None, v2: float | None = None) -> bool:
    """数值/布尔算子判定（不含分位算子）。value 为 None 或非有限数时调用方应先判为缺失。"""
    if op == "is_true":
        return bool(value) is True
    if op == "is_false":
        return bool(value) is False
    if value is None or isinstance(value, bool):
        raise ValueError(f"算子 {op} 需要数值，收到 {value!r}")
    x = float(value)
    if op == "gt":
        return x > _req(v1, op)
    if op == "gte":
        return x >= _req(v1, op)
    if op == "lt":
        return x < _req(v1, op)
    if op == "lte":
        return x <= _req(v1, op)
    if op == "between":
        lo, hi = _req(v1, op), _req(v2, op)
        if lo > hi:
            lo, hi = hi, lo
        return lo <= x <= hi
    raise ValueError(f"check_op 不处理算子 {op}")


def _req(v: float | None, op: str) -> float:
    if v is None:
        raise ValueError(f"算子 {op} 缺少阈值")
    return float(v)


def pct_rank(sorted_group: Sequence[float], v: float) -> float:
    """平均秩百分位：0=组内最小，100=组内最大；并列取平均。sorted_group 必须已升序且包含 v。

    pct = 100 × (小于 v 的个数 + 0.5 × (等于 v 的个数 − 1)) ÷ (n − 1)
    """
    n = len(sorted_group)
    if n < 2:
        return 50.0
    less = bisect_left(sorted_group, v)
    equal = bisect_right(sorted_group, v) - less
    if equal == 0:  # v 不在组内：按插入位置估计
        equal = 1
    return 100.0 * (less + 0.5 * (equal - 1)) / (n - 1)


def is_finite_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def margin(op: str, value: float, v1: float | None, v2: float | None = None) -> float | None:
    """相对距离：通过为正、未通过为负。阈值为 0 时用绝对差。"""

    def rel(x: float, t: float) -> float:
        return (x - t) / abs(t) if t != 0 else (x - t)

    if op in ("gt", "gte") and v1 is not None:
        return rel(value, v1)
    if op in ("lt", "lte") and v1 is not None:
        return -rel(value, v1)
    if op == "between" and v1 is not None and v2 is not None:
        lo, hi = min(v1, v2), max(v1, v2)
        if value < lo:
            return rel(value, lo)
        if value > hi:
            return -rel(value, hi)
        return min(rel(value, lo), -rel(value, hi))
    return None


def rank_margin(op: str, rank: float, v1: float) -> float:
    """分位算子的距离（单位：分位点 ÷ 100），通过为正。"""
    return (v1 - rank) / 100.0 if op == "pct_rank_lte" else (rank - v1) / 100.0
