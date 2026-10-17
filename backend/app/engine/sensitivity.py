"""条件影响分析：去条件影响、阈值扫描、单独卡点、差一点入选（DESIGN §5.3/§5.4）。

全部基于一次算好的“股票 × 条件”状态矩阵（EvalMatrix，由 evaluate.build_matrix 构建）：
去掉某条件或改阈值时只重判这一列，其余列复用，因此 drop_effect 与 sweep 都是精确重算而非估计。

入选规则（与 evaluate 分桶一致）：
- 任一硬条件 fail → 不入选；任一硬条件 skipped（预筛未取数）→ 无法确认，不入选；
- 缺失/不适用：missing_policy=exclude 时不入选，keep_flagged 时可带标记入选。

“未知”股票：没有 fail、但有 skipped 的条件，且缺失策略允许它入选——它到底能否入选取决于未取的数据。
某个去条件/扫描点会产生未知股票时，入选数只是下限：drop_effect 返回 None，sweep 在 note 中说明。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from .. import catalog
from ..schemas import Condition, Evidence, FeatureSnapshot, MetricDef, Strategy, SweepPoint
from .conditions import RANK_OPS, normalize, op_family
from .ops import check_op

# 状态编码（矩阵内用 int8）
PASS, FAIL, MISSING, NA, SKIPPED = 0, 1, 2, 3, 4
STATUS_NAMES: tuple[str, ...] = ("pass", "fail", "missing", "not_applicable", "skipped")

SWEEP_FACTORS: tuple[float, ...] = (0.5, 0.75, 1.25, 1.5)
RANK_SWEEP_VALUES: tuple[float, ...] = (20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0)
MAX_SWEEP_POINTS = 9
NEAR_MISS_MARGIN = 0.2  # 数值条件：|相对距离| ≤ 0.2
NEAR_MISS_RANK_POINTS = 10.0  # 分位条件：差 ≤ 10 个分位点

_RANK_AS_CMP = {"pct_rank_lte": "lte", "pct_rank_gte": "gte"}
_EPS = 1e-12


def judge(cond: Condition, x: float | bool, threshold: float | None = None) -> bool:
    """判定单个值是否满足条件；threshold 非空时替换阈值（between 替换上界）。

    数值/布尔直接调用 ops.check_op；分位算子把分位值当作数值，用 lte/gte 比较，保证与回测同一实现。
    """
    op = cond.op
    if op in RANK_OPS:
        return check_op(_RANK_AS_CMP[op], x, cond.value if threshold is None else threshold)
    if op in ("is_true", "is_false"):
        return check_op(op, x, None)
    if op == "between":
        lo, hi = cond.value, cond.value2
        if threshold is None:
            return check_op(op, x, lo, hi)
        base_lo = min(lo, hi) if lo is not None and hi is not None else lo
        return check_op(op, x, base_lo, threshold)
    return check_op(op, x, cond.value if threshold is None else threshold)


@dataclass
class CondColumn:
    """单个条件在全部股票上的判定结果（与 EvalMatrix.codes 同序）。"""

    cond: Condition
    mdef: MetricDef
    family: str  # numeric / rank / bool
    threshold: str  # threshold_text(cond)
    status: np.ndarray  # int8 状态编码
    judge_vals: list[float | bool | None]  # 与阈值比较的值（数值或分位）；None = 不参与判定（缺失/不适用/跳过）
    values: list[float | bool | None]
    displays: list[str]
    reasons: list[str | None]
    margins: list[float | None]
    ranks: list[float | None]
    group_sizes: list[int | None]
    evidences: list[Evidence | None]
    notes: list[str] = field(default_factory=list)

    def status_at(self, i: int, threshold: float) -> int:
        """换阈值后第 i 只股票的状态；不参与判定的股票状态不变。"""
        x = self.judge_vals[i]
        if x is None:
            return int(self.status[i])
        return PASS if judge(self.cond, x, threshold) else FAIL


@dataclass
class EvalMatrix:
    """股票 × 启用条件 的状态矩阵与硬条件汇总计数。"""

    strategy: Strategy  # 已规范化
    codes: list[str]  # 排序后的股票代码
    columns: list[CondColumn]  # 启用条件，按策略顺序
    keep_flagged: bool
    hard_idx: list[int] = field(init=False)
    fail_cnt: np.ndarray = field(init=False)
    skip_cnt: np.ndarray = field(init=False)
    miss_cnt: np.ndarray = field(init=False)  # missing + not_applicable

    def __post_init__(self) -> None:
        n = len(self.codes)
        self.hard_idx = [j for j, col in enumerate(self.columns) if col.cond.hard]
        self.fail_cnt = np.zeros(n, dtype=np.int32)
        self.skip_cnt = np.zeros(n, dtype=np.int32)
        self.miss_cnt = np.zeros(n, dtype=np.int32)
        for j in self.hard_idx:
            st = self.columns[j].status
            self.fail_cnt += st == FAIL
            self.skip_cnt += st == SKIPPED
            self.miss_cnt += (st == MISSING) | (st == NA)

    def index_of(self, cond_id: str) -> int:
        for j, col in enumerate(self.columns):
            if col.cond.id == cond_id:
                return j
        raise KeyError(f"条件 {cond_id} 不存在或未启用")

    def included_mask(self) -> np.ndarray:
        ok = (self.fail_cnt == 0) & (self.skip_cnt == 0)
        return ok if self.keep_flagged else ok & (self.miss_cnt == 0)

    def included_count(self) -> int:
        return int(self.included_mask().sum())

    def others(self, j: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """去掉第 j 列（硬条件）后其余硬条件的 fail / skipped / 缺失 计数。"""
        st = self.columns[j].status
        return (
            self.fail_cnt - (st == FAIL),
            self.skip_cnt - (st == SKIPPED),
            self.miss_cnt - ((st == MISSING) | (st == NA)),
        )


# ───────────────────────────── 矩阵上的算法 ─────────────────────────────


def drop_count(m: EvalMatrix, j: int) -> int | None:
    """去掉第 j 个条件后的入选数（精确）；会产生“未知”股票时返回 None。软条件不影响入选数。"""
    if not m.columns[j].cond.hard:
        return m.included_count()
    f, s, mi = m.others(j)
    base = (f == 0) & (m.keep_flagged | (mi == 0))
    if bool((base & (s > 0)).any()):
        return None
    return int((base & (s == 0)).sum())


def sole_blocker_count(m: EvalMatrix, j: int) -> int:
    """只因该条件 fail 而被排除的股票数：其余硬条件都满足（或在 keep_flagged 下可带标记入选）。"""
    col = m.columns[j]
    if not col.cond.hard:
        return 0
    f, s, mi = m.others(j)
    ok_others = (f == 0) & (s == 0) & (m.keep_flagged | (mi == 0))
    return int((ok_others & (col.status == FAIL)).sum())


def sweep_points(m: EvalMatrix, j: int, values: Sequence[float]) -> tuple[list[SweepPoint], bool]:
    """第 j 个条件取不同阈值时的入选数；返回 (扫描点, 是否存在下限点)。软/布尔条件入选数不变。"""
    col = m.columns[j]
    current = current_threshold(col.cond)
    pts: list[SweepPoint] = []
    if not col.cond.hard or col.family == "bool":
        n_inc = m.included_count()
        return [SweepPoint(value=float(v), included=n_inc, is_current=_is_current(v, current)) for v in values], False
    f, s, mi = m.others(j)
    cand = np.nonzero((f == 0) & (m.keep_flagged | (mi == 0)))[0].tolist()
    other_skip = s.tolist()
    lower_bound = False
    for v in values:
        t = float(v)
        n_inc = 0
        for i in cand:
            st = col.status_at(i, t)
            if st == FAIL or (not m.keep_flagged and st in (MISSING, NA)):
                continue
            if st == SKIPPED or other_skip[i] > 0:
                lower_bound = True
                continue
            n_inc += 1
        pts.append(SweepPoint(value=t, included=n_inc, is_current=_is_current(t, current)))
    return pts, lower_bound


def near_miss_mask(m: EvalMatrix) -> np.ndarray:
    """差一点入选：恰好 1 个硬条件 fail、其余都不阻碍入选，且该条件 |margin| ≤ 0.2（分位差 ≤ 10 个点）。"""
    n = len(m.codes)
    out = np.zeros(n, dtype=bool)
    if not m.hard_idx:
        return out
    one = (m.fail_cnt == 1) & (m.skip_cnt == 0)
    if not m.keep_flagged:
        one &= m.miss_cnt == 0
    for i in np.nonzero(one)[0].tolist():
        for j in m.hard_idx:
            col = m.columns[j]
            if col.status[i] != FAIL:
                continue
            mg = col.margins[i]
            tol = NEAR_MISS_RANK_POINTS / 100.0 if col.family == "rank" else NEAR_MISS_MARGIN
            out[i] = mg is not None and abs(mg) <= tol + _EPS
            break
    return out


# ───────────────────────────── 扫描点 ─────────────────────────────


def current_threshold(cond: Condition) -> float | None:
    """被扫描的阈值：between 取上界（较大者），其余取 value。"""
    if cond.op == "between":
        if cond.value is None or cond.value2 is None:
            return None
        return float(max(cond.value, cond.value2))
    return float(cond.value) if cond.value is not None else None


def _is_current(v: float, current: float | None) -> bool:
    return current is not None and math.isclose(float(v), current, rel_tol=1e-9, abs_tol=1e-9)


def _round_sig(x: float) -> float:
    return float(f"{x:.6g}")


def sweep_values_for(cond: Condition, mdef: MetricDef | None = None) -> list[float]:
    """扫描点：数值算子 = catalog.sweep_values ∪ 当前阈值 ×{0.5,0.75,1.25,1.5}，落在 plausible_range 内，
    去重排序，最多 9 点（以当前阈值为中心截取）；between 扫上界；分位算子 = 20..80（含当前值）；布尔无扫描。"""
    mdef = mdef or catalog.METRICS.get(cond.metric)
    fam = op_family(cond.op)
    cur = current_threshold(cond)
    if mdef is None or fam not in ("numeric", "rank") or cur is None or not math.isfinite(cur):
        return []
    if fam == "rank":
        rank_vals = set(RANK_SWEEP_VALUES)
        if 0.0 <= cur <= 100.0:
            rank_vals.add(cur)
        return sorted(rank_vals)
    cands = {float(v) for v in mdef.sweep_values} | {_round_sig(cur * f) for f in SWEEP_FACTORS}
    if mdef.plausible_range is not None:
        lo, hi = mdef.plausible_range
        cands = {v for v in cands if lo <= v <= hi}
    if cond.op == "between" and cond.value is not None and cond.value2 is not None:
        floor = float(min(cond.value, cond.value2))
        cands = {v for v in cands if v >= floor}
    cands.add(cur)
    vals = sorted(cands)
    if len(vals) > MAX_SWEEP_POINTS:
        idx = vals.index(cur)
        start = max(0, min(idx - MAX_SWEEP_POINTS // 2, len(vals) - MAX_SWEEP_POINTS))
        vals = vals[start:start + MAX_SWEEP_POINTS]
    return vals


# ───────────────────────────── 对外接口 ─────────────────────────────


def _matrix_for(strategy: Strategy, snap: FeatureSnapshot, cond_id: str) -> tuple[EvalMatrix, int]:
    """构建矩阵并定位条件；停用的条件按“启用后”计算。"""
    from .evaluate import build_matrix  # 局部导入：evaluate 依赖本模块，避免循环导入

    ns = normalize(strategy)
    target = next((c for c in ns.conditions if c.id == cond_id), None)
    if target is None:
        raise KeyError(f"条件 {cond_id} 不存在")
    if not target.enabled:
        target.enabled = True
    m = build_matrix(ns, snap)
    return m, m.index_of(cond_id)


def threshold_sweep(strategy: Strategy, snap: FeatureSnapshot, cond_id: str, values: Sequence[float]) -> list[SweepPoint]:
    """把条件 cond_id 的阈值（between 为上界）依次换成 values 时的入选数。

    停用的条件按“启用并取该阈值”计算；软条件不影响入选数；布尔条件没有阈值可扫描（ValueError）。
    """
    m, j = _matrix_for(strategy, snap, cond_id)
    if m.columns[j].family == "bool":
        raise ValueError(f"条件 {cond_id} 为布尔条件，没有可扫描的阈值")
    pts, _ = sweep_points(m, j, values)
    return pts


def drop_effect(strategy: Strategy, snap: FeatureSnapshot, cond_id: str) -> int | None:
    """去掉条件 cond_id 后的入选数（精确重算）；预筛模式下无法精确时返回 None。"""
    from .evaluate import build_matrix

    ns = normalize(strategy)
    target = next((c for c in ns.conditions if c.id == cond_id), None)
    if target is None:
        raise KeyError(f"条件 {cond_id} 不存在")
    m = build_matrix(ns, snap)
    if not target.enabled:
        return m.included_count()
    return drop_count(m, m.index_of(cond_id))
