"""筛选任务编排（DESIGN §7.1）：后台运行、进度、快照复用、what-if、个股详情、比较与解释。

运行流程：validate_strategy（error → failed）→ resolve_universe → 取数建快照（all_a 两段式预筛）→ evaluate →
组装 ScreenResult（数据集状态、漏斗、三个分组、条件统计、运行期冲突、告警）→ 模板摘要 → LLM 摘要（可选，失败保留模板）。
任何异常都以 status=failed + 中文 error 结束，绝不挂起；缺失/失败的数据永远不会被当作“通过”。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import catalog
from ..catalog import CHEAP_DATASETS, DATASETS, DISPLAY_METRICS, METRICS, datasets_for
from ..config import Settings
from ..data.features import bars_from_response
from ..data.fuyao import NO_KEY_MESSAGE, FuyaoError
from ..data.periods import history_window, now_cn, today_cn
from ..engine.conflicts import validate_strategy
from ..engine.diff import diff_results
from ..engine.evaluate import InvalidStrategyError, evaluate, prefilter
from ..schemas import (
    CompareRequest,
    CompareResult,
    CompareRow,
    EvaluationOutput,
    Explanation,
    ExplanationSentence,
    FeatureSnapshot,
    PricePoint,
    ScreenRequest,
    ScreenResult,
    SourceRef,
    StockDetail,
    StockInfo,
    StockResult,
    Strategy,
    WhatIfResult,
)

log = logging.getLogger(__name__)

MAX_RUNS = 50
SNAPSHOT_TTL_S = 600.0
SUMMARY_LLM_TIMEOUT_S = 20.0
STOCK_LLM_TIMEOUT_S = 30.0
PRICE_POINTS = 250
CHEAP_METRICS: frozenset[str] = frozenset(m for m, d in METRICS.items() if set(d.datasets) <= CHEAP_DATASETS)
RANK_REF_SCOPES = ("industry", "market")

NO_FUYAO_KEY_ERROR = "未配置扶摇 API Key（FUYAO_API_KEY），无法获取行情与财务数据：请在项目根目录 .env 中配置后重启后端。"
FAULT_DEMO_WARNING = "本次为故障演示：已模拟以下数据集接口不可用（未发出真实请求）：{names}；受影响条件按“数据缺失”处理，不会判为通过。"


class RunError(Exception):
    """可预期的运行失败（中文提示直接展示给用户）。"""


class NotFoundError(LookupError):
    pass


@dataclass
class RunRecord:
    run_id: str
    request: ScreenRequest
    result: ScreenResult
    snapshot: FeatureSnapshot | None = None
    evaluation: EvaluationOutput | None = None
    faults: frozenset[str] = frozenset()
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def fuyao_error_text(e: FuyaoError) -> str:
    if NO_KEY_MESSAGE in str(e):
        return NO_FUYAO_KEY_ERROR
    try:
        return e.describe()
    except Exception:  # noqa: BLE001
        return str(e)


def error_text(e: BaseException) -> str:
    if isinstance(e, RunError):
        return str(e)
    if isinstance(e, FuyaoError):
        return f"数据接口调用失败：{fuyao_error_text(e)}"
    if isinstance(e, InvalidStrategyError):
        return str(e)
    if isinstance(e, ValueError):
        return str(e) or "参数不合法"
    if isinstance(e, asyncio.TimeoutError):
        return "运行超时，请稍后重试或缩小股票池"
    return f"运行异常：{type(e).__name__}{'：' + str(e)[:200] if str(e) else ''}"


def _universe_key(s: Strategy) -> tuple[Any, ...]:
    u = s.universe
    return (u.kind, (u.code or "").upper(), tuple(sorted(u.codes)) if u.kind == "custom" else (), u.query if not u.code else None)


def _enabled_metrics(s: Strategy) -> set[str]:
    return {c.metric for c in s.conditions if c.enabled and c.metric in METRICS}


def _ref_metrics(s: Strategy) -> set[str]:
    return {c.metric for c in s.conditions if c.enabled and c.metric in METRICS and c.rank_scope in RANK_REF_SCOPES}


def _all_stock_results(out: EvaluationOutput | None) -> list[StockResult]:
    if out is None:
        return []
    return [*out.included, *out.excluded, *out.missing]


# ───────────────────────────── 解释（AI 模块不可用时的兜底） ─────────────────────────────


def _explain_module() -> Any | None:
    try:
        from ..ai import explain  # noqa: PLC0415 — 由另一模块并行实现，缺失时降级

        return explain
    except Exception:  # noqa: BLE001
        log.warning("ai.explain 不可用，使用内置简易模板", exc_info=True)
        return None


def basic_run_summary(result: ScreenResult) -> Explanation:
    size = result.universe.size if result.universe else 0
    s = [ExplanationSentence(
        text=f"股票池共 {size} 只，入选 {len(result.included)} 只、排除 {len(result.excluded)} 只、数据缺失 {len(result.missing)} 只。",
        kind="fact",
    )]
    if result.trade_date:
        s.append(ExplanationSentence(text=f"价格类数据截至 {result.trade_date}。", kind="fact"))
    s.append(ExplanationSentence(text="结果仅为按条件的历史与当期数据筛选，不构成投资建议，不代表未来表现。", kind="caveat"))
    return Explanation(sentences=s, generator="template", verified=True)


def basic_stock_explanation(stock: StockResult) -> Explanation:
    s: list[ExplanationSentence] = []
    for ev in stock.evaluations:
        status = {"pass": "满足", "fail": "不满足", "missing": "数据缺失", "not_applicable": "不适用", "skipped": "未评估"}.get(ev.status, ev.status)
        kind = "fact" if ev.status in ("pass", "fail") else "uncertain"
        s.append(ExplanationSentence(text=f"{ev.condition_id}：当前值 {ev.display}，条件 {ev.threshold}，{status}。", kind=kind, refs=[ev.condition_id]))
    s.append(ExplanationSentence(text="以上为数据事实与条件判定，不构成投资建议。", kind="caveat"))
    return Explanation(sentences=s, generator="template", verified=True)


# ───────────────────────────── RunManager ─────────────────────────────


class RunManager:
    def __init__(self, settings: Settings, repository: Any, llm: Any | None = None) -> None:
        self.settings = settings
        self.repo = repository
        self.llm = llm
        self._runs: OrderedDict[str, RunRecord] = OrderedDict()
        self._snap_cache: dict[tuple[Any, ...], tuple[float, FeatureSnapshot]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ── 基础 ──

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm is not None and getattr(self.llm, "enabled", False))

    def _fuyao_missing(self) -> bool:
        client = getattr(self.repo, "client", None)
        return client is not None and not getattr(client, "enabled", True)

    def _remember(self, rec: RunRecord) -> None:
        self._runs[rec.run_id] = rec
        self._runs.move_to_end(rec.run_id)
        while len(self._runs) > MAX_RUNS:
            old_id, old = next(iter(self._runs.items()))
            if old.task is not None and not old.task.done():
                break  # 不丢弃仍在运行的任务
            self._runs.pop(old_id, None)

    def record(self, run_id: str) -> RunRecord:
        rec = self._runs.get(run_id)
        if rec is None:
            raise NotFoundError("运行记录不存在（可能已过期或后端已重启），请重新执行筛选")
        return rec

    def get(self, run_id: str) -> ScreenResult:
        return self.record(run_id).result

    def start(self, req: ScreenRequest) -> str:
        run_id = _new_run_id()
        result = ScreenResult(
            run_id=run_id, status="pending", progress=0.0, progress_text="排队中", strategy=req.strategy,
            simulated_failures=list(req.simulate_failures), created_at=now_cn(),
        )
        rec = RunRecord(run_id=run_id, request=req, result=result, faults=frozenset(req.simulate_failures))
        self._remember(rec)
        rec.task = asyncio.create_task(self._execute(rec))
        self._tasks.add(rec.task)
        rec.task.add_done_callback(self._tasks.discard)
        return run_id

    async def prime(self, strategy: Strategy, *, progress: Callable[[float, str], None] | None = None) -> FeatureSnapshot:
        """预热：为策略建快照并写入 10 分钟复用缓存（不产生运行记录）。"""
        vr = validate_strategy(strategy)
        if self._fuyao_missing():
            raise RunError(NO_FUYAO_KEY_ERROR)
        return await self._snapshot_for(vr.strategy, frozenset(), progress=progress)

    async def wait(self, run_id: str, timeout: float | None = None) -> ScreenResult:
        rec = self.record(run_id)
        await asyncio.wait_for(rec.done.wait(), timeout)
        return rec.result

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except BaseException:  # noqa: BLE001
                pass

    # ── 主流程 ──

    async def _execute(self, rec: RunRecord) -> None:
        res = rec.result
        try:
            res.status = "running"
            self._progress(res, 0.01, "校验条件")
            vr = validate_strategy(rec.request.strategy)
            res.strategy = vr.strategy
            errors = [c for c in vr.conflicts if c.severity == "error"]
            if errors:
                res.conflicts = vr.conflicts
                raise RunError("条件存在冲突，无法执行：" + "；".join(c.message for c in errors))
            strategy = vr.strategy
            if self._fuyao_missing():
                raise RunError(NO_FUYAO_KEY_ERROR)

            def prog(frac: float, text: str) -> None:
                self._progress(res, 0.05 + 0.80 * max(0.0, min(1.0, frac)), text)

            snap = await self._snapshot_for(strategy, rec.faults, force_refresh=rec.request.force_refresh, progress=prog)
            self._progress(res, 0.88, "逐股判定与影响分析")
            out = await asyncio.to_thread(evaluate, strategy, snap)
            rec.snapshot, rec.evaluation = snap, out
            self._fill_result(res, strategy, snap, out, vr.conflicts, rec.request.simulate_failures)
            self._progress(res, 0.92, "生成摘要")
            res.summary = await self._summary(res)
            res.status = "done"
            self._progress(res, 1.0, "完成")
        except asyncio.CancelledError:
            res.status, res.error = "failed", "运行被取消（服务正在关闭）"
            raise
        except Exception as e:  # noqa: BLE001 — 任何异常都要落到 failed，不能挂起
            if not isinstance(e, (RunError, FuyaoError, ValueError)):
                log.exception("筛选运行异常 run_id=%s", rec.run_id)
            res.status = "failed"
            res.error = error_text(e)
            res.progress_text = "运行失败"
        finally:
            res.finished_at = now_cn()
            rec.done.set()

    @staticmethod
    def _progress(res: ScreenResult, p: float, text: str) -> None:
        res.progress = round(max(res.progress, min(1.0, p)), 4)
        res.progress_text = text

    def _cache_key(self, strategy: Strategy, datasets: set[str], tag: str = "") -> tuple[Any, ...]:
        report = strategy.report_period or "auto"
        return (_universe_key(strategy), frozenset(datasets), report, today_cn().isoformat(), tag)

    def _cache_get(self, key: tuple[Any, ...]) -> FeatureSnapshot | None:
        hit = self._snap_cache.get(key)
        if hit is None:
            return None
        if time.monotonic() - hit[0] > SNAPSHOT_TTL_S:
            self._snap_cache.pop(key, None)
            return None
        return hit[1]

    def _cache_put(self, key: tuple[Any, ...], snap: FeatureSnapshot) -> None:
        now = time.monotonic()
        for k in [k for k, (t, _) in self._snap_cache.items() if now - t > SNAPSHOT_TTL_S]:
            self._snap_cache.pop(k, None)
        self._snap_cache[key] = (now, snap)

    def _covers(self, snap: FeatureSnapshot, strategy: Strategy, metrics: set[str]) -> bool:
        if not metrics <= set(snap.metrics_available):
            return False
        return all(m in snap.reference for m in _ref_metrics(strategy) if m in CHEAP_METRICS)

    async def _snapshot_for(
        self,
        strategy: Strategy,
        faults: frozenset[str],
        *,
        force_refresh: bool = False,
        progress: Callable[[float, str], None] | None = None,
    ) -> FeatureSnapshot:
        prog = progress or (lambda _f, _t: None)
        reuse = not faults
        prog(0.0, "解析股票池")
        universe = await self.repo.resolve_universe(strategy.universe, faults=faults)
        if universe.size == 0 or not universe.members:
            raise RunError(f"股票池“{universe.name}”没有可用成分股，请换一个股票池")
        prog(0.02, f"股票池：{universe.name}（{universe.size} 只）")
        wanted = _enabled_metrics(strategy) | set(DISPLAY_METRICS)

        if strategy.universe.kind != "all_a":
            key = self._cache_key(strategy, datasets_for(wanted))
            cached = self._cache_get(key) if reuse and not force_refresh else None
            if cached is not None and self._covers(cached, strategy, wanted):
                prog(1.0, "复用 10 分钟内的数据快照")
                return cached
            if cached is not None:
                snap = await self.repo.build_snapshot(strategy, faults=faults, progress=prog, base=cached)
            else:
                snap = await self.repo.build_snapshot(strategy, faults=faults, progress=prog)
            if reuse:
                self._cache_put(key, snap)
            return snap

        # 全A：先只取廉价数据集预筛，再对剩余股票深度取数
        cheap_wanted = wanted & CHEAP_METRICS
        deep_wanted = wanted - CHEAP_METRICS

        def cheap_prog(f: float, t: str) -> None:
            prog(0.4 * f, f"全A预筛：{t}")

        key = self._cache_key(strategy, datasets_for(cheap_wanted), "all_a_cheap")
        cheap = self._cache_get(key) if reuse and not force_refresh else None
        if cheap is None or not self._covers(cheap, strategy, cheap_wanted):
            cheap = await self.repo.build_snapshot(
                strategy, faults=faults, progress=cheap_prog, metrics=cheap_wanted, base=cheap
            ) if cheap is not None else await self.repo.build_snapshot(
                strategy, faults=faults, progress=cheap_prog, metrics=cheap_wanted
            )
            if reuse:
                self._cache_put(key, cheap)
        if not deep_wanted:
            return cheap
        remaining = prefilter(strategy, cheap, set(CHEAP_METRICS))
        limit = int(self.settings.max_deep_eval)
        if len(remaining) > limit:
            raise RunError(
                f"全A预筛后仍有 {len(remaining)} 只，超过深度评估上限 {limit}，请增加估值/行情类条件或换更小的股票池"
            )
        prog(0.4, f"全A预筛后剩余 {len(remaining)} 只，开始深度取数")

        def deep_prog(f: float, t: str) -> None:
            prog(0.4 + 0.6 * f, t)

        base = cheap.model_copy(update={"prefiltered": True})
        snap = await self.repo.build_snapshot(
            strategy, faults=faults, progress=deep_prog, base=base, members=remaining, metrics=deep_wanted
        )
        if not snap.prefiltered:
            snap = snap.model_copy(update={"prefiltered": True})
        return snap

    def _fill_result(
        self,
        res: ScreenResult,
        strategy: Strategy,
        snap: FeatureSnapshot,
        out: EvaluationOutput,
        static_conflicts: list[Any],
        simulated: list[str],
    ) -> None:
        res.strategy = strategy
        res.strategy_hash = out.strategy_hash
        res.universe = snap.universe
        res.report_period = snap.report_period
        res.trade_date = snap.trade_date
        res.datasets = list(snap.datasets)
        res.funnel = out.funnel
        res.included, res.excluded, res.missing = out.included, out.excluded, out.missing
        res.condition_stats = out.condition_stats
        seen: set[str] = set()
        conflicts = []
        for c in [*static_conflicts, *out.conflicts]:
            if c.id not in seen:
                seen.add(c.id)
                conflicts.append(c)
        res.conflicts = conflicts
        res.warnings = self._warnings(strategy, snap, simulated)
        res.simulated_failures = list(simulated)

    @staticmethod
    def _warnings(strategy: Strategy, snap: FeatureSnapshot, simulated: list[str]) -> list[str]:
        out: list[str] = []
        if simulated:
            names = "、".join(DATASETS.get(d, (d, ""))[0] for d in simulated)
            out.append(FAULT_DEMO_WARNING.format(names=names))
        enabled = [c for c in strategy.conditions if c.enabled and c.metric in METRICS]
        for ds in snap.datasets:
            if ds.status not in ("partial", "failed"):
                continue
            affected = [c.label or c.metric for c in enabled if ds.dataset in METRICS[c.metric].datasets]
            if ds.dataset in ("tickers", "industry_map"):
                affected = affected or ["（股票名称/行业归属）"]
            what = "部分失败" if ds.status == "partial" else "失败"
            stat = f"（成功 {ds.succeeded}/{ds.requested}）" if ds.requested else ""
            out.append(f"{ds.label} 数据{what}{stat}，受影响条件：{'、'.join(affected) if affected else '无（仅影响展示指标）'}；相关股票按数据缺失处理")
        if snap.prefiltered:
            out.append("全A模式：先用估值/行情等廉价条件预筛，未通过预筛的股票不再深度取数（显示为“已被前置条件排除，未取数”）。")
        return out

    async def _summary(self, res: ScreenResult) -> Explanation:
        mod = _explain_module()
        try:
            summary = mod.template_run_summary(res) if mod is not None else basic_run_summary(res)
        except Exception:  # noqa: BLE001
            log.warning("模板摘要生成失败", exc_info=True)
            summary = basic_run_summary(res)
        if mod is not None and self.llm_enabled and hasattr(mod, "llm_run_summary"):
            try:
                summary = await asyncio.wait_for(mod.llm_run_summary(res, self.llm), SUMMARY_LLM_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 — 失败保留模板
                note = "AI 摘要超时，已使用模板摘要" if isinstance(e, asyncio.TimeoutError) else f"AI 摘要不可用，已使用模板摘要（{type(e).__name__}）"
                summary = summary.model_copy(update={"verification_notes": [*summary.verification_notes, note]})
        return summary

    # ── what-if ──

    async def whatif(self, run_id: str, strategy: Strategy) -> WhatIfResult:
        rec = self.record(run_id)
        if rec.result.status != "done" or rec.snapshot is None or rec.evaluation is None:
            raise RunError("该运行尚未完成或已失败，无法进行 what-if 分析")
        vr = validate_strategy(strategy)
        errors = [c for c in vr.conflicts if c.severity == "error"]
        if errors:
            raise RunError("条件存在冲突，无法执行：" + "；".join(c.message for c in errors))
        ns = vr.strategy
        base_strategy = rec.result.strategy
        faults = rec.faults
        same_pool = _universe_key(ns) == _universe_key(base_strategy) and (ns.report_period or None) == (base_strategy.report_period or None)
        if not same_pool:
            snap = await self._snapshot_for(ns, faults)
        else:
            snap = await self._extend_snapshot(rec.snapshot, ns, faults)
        out = await asyncio.to_thread(evaluate, ns, snap)
        new_id = _new_run_id()
        res = ScreenResult(
            run_id=new_id, status="done", progress=1.0, progress_text="完成（what-if）", strategy=ns,
            created_at=now_cn(), simulated_failures=list(rec.request.simulate_failures),
        )
        self._fill_result(res, ns, snap, out, vr.conflicts, list(rec.request.simulate_failures))
        res.summary = await self._summary(res)
        res.finished_at = now_cn()
        new_rec = RunRecord(
            run_id=new_id, request=ScreenRequest(strategy=ns, simulate_failures=list(rec.request.simulate_failures)),
            result=res, snapshot=snap, evaluation=out, faults=faults,
        )
        new_rec.done.set()
        self._remember(new_rec)
        diff = diff_results(rec.evaluation, out, base_strategy, ns, base_run_id=run_id, new_run_id=new_id)
        return WhatIfResult(result=res, diff=diff)

    async def _extend_snapshot(self, snap: FeatureSnapshot, strategy: Strategy, faults: frozenset[str]) -> FeatureSnapshot:
        wanted = _enabled_metrics(strategy) | set(DISPLAY_METRICS)
        if not snap.prefiltered:
            if self._covers(snap, strategy, wanted):
                return snap
            return await self.repo.build_snapshot(strategy, faults=faults, base=snap)
        # 预筛快照：先补廉价指标（全体），再按新条件预筛并为剩余股票补深度指标
        cheap_wanted = wanted & CHEAP_METRICS
        deep_wanted = wanted - CHEAP_METRICS
        if not self._covers(snap, strategy, cheap_wanted):
            snap = await self.repo.build_snapshot(strategy, faults=faults, base=snap, metrics=cheap_wanted)
        if not deep_wanted:
            return snap
        remaining = prefilter(strategy, snap, set(CHEAP_METRICS))
        limit = int(self.settings.max_deep_eval)
        if len(remaining) > limit:
            raise RunError(f"全A预筛后仍有 {len(remaining)} 只，超过深度评估上限 {limit}，请增加估值/行情类条件或换更小的股票池")
        lacking = [c for c in remaining if any(m not in snap.stocks[c].metrics for m in deep_wanted)]
        if lacking:
            snap = await self.repo.build_snapshot(strategy, faults=faults, base=snap, members=lacking, metrics=deep_wanted)
        return snap if snap.prefiltered else snap.model_copy(update={"prefiltered": True})

    # ── 个股 / 比较 / 解释 ──

    def _done_record(self, run_id: str) -> RunRecord:
        rec = self.record(run_id)
        if rec.snapshot is None or rec.evaluation is None:
            raise RunError("该运行尚未完成或已失败，暂无个股数据")
        return rec

    async def _prices(self, thscode: str) -> tuple[list[PricePoint], SourceRef | None, str | None]:
        try:
            start, end = history_window(today_cn())
            resp = await self.repo.history(thscode, start, end, "backward")
            bars = bars_from_response(resp)[-PRICE_POINTS:]
            pts = [PricePoint(date=b.day.isoformat(), close=float(b.close)) for b in bars if b.close is not None]
            note = f"后复权收盘价（最近 {len(pts)} 个交易日，已剔除停牌日）" if pts else "接口未返回有效日K数据"
            return pts, resp.source("close_price", thscode=thscode), note
        except FuyaoError as e:
            return [], e.source(thscode=thscode) if hasattr(e, "source") else None, f"价格走势获取失败：{fuyao_error_text(e)}"
        except Exception as e:  # noqa: BLE001
            return [], None, f"价格走势获取失败：{type(e).__name__}"

    @staticmethod
    def _find_stock(rec: RunRecord, thscode: str) -> StockResult | None:
        for sr in _all_stock_results(rec.evaluation):
            if sr.thscode == thscode:
                return sr
        return None

    async def stock_detail(self, run_id: str, thscode: str) -> StockDetail:
        rec = self._done_record(run_id)
        code = thscode.strip().upper()
        st = rec.snapshot.stocks.get(code) if rec.snapshot else None
        if st is None:
            raise NotFoundError(f"股票 {code} 不在本次运行的股票池中")
        sr = self._find_stock(rec, code)
        prices, src, note = await self._prices(code)
        return StockDetail(
            info=st.info, bucket=sr.bucket if sr else None, metrics=dict(st.metrics),
            evaluations=list(sr.evaluations) if sr else [], prices=prices, price_source=src, price_note=note,
        )

    async def compare(self, req: CompareRequest) -> CompareResult:
        rec = self._done_record(req.run_id)
        snap = rec.snapshot
        assert snap is not None
        codes = list(dict.fromkeys(c.strip().upper() for c in req.thscodes if c and c.strip()))
        if not codes:
            raise RunError("请至少选择 1 只股票进行比较")
        if len(codes) > 10:
            raise RunError("一次最多比较 10 只股票")
        unknown = [c for c in codes if c not in snap.stocks]
        if unknown:
            raise NotFoundError(f"以下股票不在本次运行的股票池中：{'、'.join(unknown)}")
        strategy = rec.result.strategy
        order: list[str] = []
        for c in strategy.conditions:
            if c.enabled and c.metric in METRICS and c.metric not in order:
                order.append(c.metric)
        for m in DISPLAY_METRICS:
            if m not in order:
                order.append(m)
        available = set(snap.metrics_available)
        rows: list[CompareRow] = []
        for m in order:
            if m not in available:
                continue
            mdef = METRICS[m]
            values: list[str | None] = []
            raw: list[float | None] = []
            period: str | None = None
            for code in codes:
                mv = snap.stocks[code].metrics.get(m)
                if mv is None:
                    values.append("未取数")
                    raw.append(None)
                    continue
                if mv.status == "ok" and mv.evidence is not None:
                    values.append(mv.evidence.display)
                    period = period or mv.evidence.period
                elif mv.status == "ok":
                    values.append(str(mv.value))
                else:
                    values.append({"missing": "缺失", "not_applicable": "不适用", "error": "取数失败"}.get(mv.status, mv.status))
                v = mv.value
                raw.append(float(v) if mv.status == "ok" and isinstance(v, (int, float)) and not isinstance(v, bool) else None)
            rows.append(CompareRow(metric=m, label=mdef.name, unit=mdef.unit, period=period, values=values, raw=raw))

        fetched = await asyncio.gather(*(self._prices(c) for c in codes))
        notes = ["指标取自本次运行的数据快照（与筛选同一时点）；走势为后复权收盘价，按共同区间首日归一为 100。"]
        series: dict[str, list[PricePoint]] = {}
        for code, (pts, _src, note) in zip(codes, fetched):
            if pts:
                series[code] = pts
            else:
                notes.append(f"{code}：{note or '无价格数据'}")
        prices: dict[str, list[PricePoint]] = {}
        if series:
            start = max(p[0].date for p in series.values())
            for code, pts in series.items():
                window = [p for p in pts if p.date >= start]
                if not window or window[0].close <= 0:
                    continue
                base = window[0].close
                prices[code] = [PricePoint(date=p.date, close=round(p.close / base * 100.0, 4)) for p in window]
        notes.append("比较仅展示历史与当期数据，不构成投资建议。")
        infos: list[StockInfo] = [snap.stocks[c].info for c in codes]
        return CompareResult(stocks=infos, rows=rows, prices=prices, notes=notes)

    async def explain_stock(self, run_id: str, thscode: str) -> Explanation:
        rec = self._done_record(run_id)
        code = thscode.strip().upper()
        sr = self._find_stock(rec, code)
        if sr is None:
            raise NotFoundError(f"股票 {code} 不在本次运行结果中")
        strategy = rec.result.strategy
        mod = _explain_module()
        try:
            exp = mod.template_stock_explanation(sr, strategy) if mod is not None else basic_stock_explanation(sr)
        except Exception:  # noqa: BLE001
            log.warning("模板解释生成失败", exc_info=True)
            exp = basic_stock_explanation(sr)
        if mod is not None and self.llm_enabled and hasattr(mod, "llm_stock_explanation"):
            try:
                exp = await asyncio.wait_for(mod.llm_stock_explanation(sr, strategy, self.llm), STOCK_LLM_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                note = "AI 解释超时，已使用模板解释" if isinstance(e, asyncio.TimeoutError) else f"AI 解释不可用，已使用模板解释（{type(e).__name__}）"
                exp = exp.model_copy(update={"verification_notes": [*exp.verification_notes, note]})
        return exp


__all__ = ["RunManager", "RunError", "NotFoundError", "error_text", "fuyao_error_text", "catalog"]
