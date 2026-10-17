"""全部 REST 路由（DESIGN §7.3，前缀 /api）。路径与载荷以 frontend/src/api.ts 为准。

错误统一为 HTTP 4xx/5xx + {detail: 中文}；业务性的数据缺失不是 HTTP 错误，而是结果里的显式状态。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from .. import catalog
from ..ai import fallback
from ..data.fuyao import FuyaoError
from ..data.ifind import get_stock_news
from ..data.periods import now_cn
from ..engine.conflicts import validate_strategy
from ..schemas import (
    BacktestRequest,
    BacktestResult,
    CatalogResponse,
    CompareRequest,
    CompareResult,
    Explanation,
    HealthStatus,
    Monitor,
    NewsResult,
    ParseRequest,
    ParseResult,
    SavedStrategy,
    ScreenRequest,
    ScreenResult,
    ServiceStatus,
    StockDetail,
    Strategy,
    UniverseOption,
    ValidateResult,
    WhatIfResult,
)
from ..services.monitor import next_run_at
from ..services.runs import NotFoundError, RunError, error_text, fuyao_error_text

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

FUYAO_TTL_S = 300.0
LLM_TTL_S = 600.0
IFIND_TTL_S = 600.0
CHECK_TIMEOUT_S = 10.0


def _state(request: Request) -> Any:
    return request.app.state


def _raise(e: Exception) -> None:
    if isinstance(e, NotFoundError):
        raise HTTPException(404, str(e)) from e
    if isinstance(e, (RunError, ValueError)):
        raise HTTPException(400, str(e) or "请求参数有误") from e
    if isinstance(e, FuyaoError):
        raise HTTPException(502, f"数据接口调用失败：{fuyao_error_text(e)}") from e
    log.exception("接口异常")
    raise HTTPException(500, error_text(e)) from e


# ───────────────────────────── 健康检查 ─────────────────────────────


async def _cached_check(state: Any, name: str, ttl: float, fn: Any) -> ServiceStatus:
    cache: dict[str, tuple[float, ServiceStatus]] = state.health_cache
    hit = cache.get(name)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        status = await asyncio.wait_for(fn(), CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        status = ServiceStatus(name=name, configured=True, ok=False, detail=f"检查超时（>{CHECK_TIMEOUT_S:g} 秒）", checked_at=now_cn())
    except Exception as e:  # noqa: BLE001 — 任何检查异常都不能让 /health 500
        status = ServiceStatus(name=name, configured=True, ok=False, detail=f"检查失败：{type(e).__name__}", checked_at=now_cn())
    cache[name] = (time.monotonic(), status)
    return status


async def _check_fuyao(state: Any) -> ServiceStatus:
    s = state.settings
    if not s.fuyao_enabled:
        return ServiceStatus(name="扶摇", configured=False, ok=False, detail="未配置 FUYAO_API_KEY，无法获取行情与财务数据")

    async def run() -> ServiceStatus:
        try:
            resp = await state.repository.trading_days()
            detail = "交易日历接口可用" + ("（缓存）" if getattr(resp, "cache_hit", False) else "")
            return ServiceStatus(name="扶摇", configured=True, ok=True, detail=detail, checked_at=now_cn())
        except FuyaoError as e:
            return ServiceStatus(name="扶摇", configured=True, ok=False, detail=fuyao_error_text(e), checked_at=now_cn())

    return await _cached_check(state, "扶摇", FUYAO_TTL_S, run)


async def _check_llm(state: Any) -> ServiceStatus:
    s = state.settings
    llm = state.llm
    if llm is None or not getattr(llm, "enabled", False):
        return ServiceStatus(name="AI", configured=False, ok=None, detail="未配置 LLM_API_KEY：解析将使用规则词典，摘要使用模板")

    async def run() -> ServiceStatus:
        url = s.llm_base_url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
                r = await client.get(url, headers={"Authorization": f"Bearer {s.llm_api_key}"})
            ok = r.status_code < 400
            detail = f"{s.llm_model} 可用" if ok else f"模型列表接口返回 HTTP {r.status_code}"
        except httpx.HTTPError as e:
            ok, detail = False, f"网络错误（{type(e).__name__}）"
        return ServiceStatus(name="AI", configured=True, ok=ok, detail=detail, checked_at=now_cn())

    return await _cached_check(state, "AI", LLM_TTL_S, run)


async def _check_ifind(state: Any) -> ServiceStatus:
    client = state.ifind
    if client is None or not getattr(client, "enabled", False):
        return ServiceStatus(name="iFinD", configured=False, ok=None, detail="未配置 IFIND_MCP_TOKEN：个股资讯不可用（不影响筛选）")

    async def run() -> ServiceStatus:
        try:
            tools = await client.list_tools(refresh=True)
            return ServiceStatus(name="iFinD", configured=True, ok=True, detail=f"MCP 可用（{len(tools)} 个工具）", checked_at=now_cn())
        except Exception as e:  # noqa: BLE001
            return ServiceStatus(name="iFinD", configured=True, ok=False, detail=str(e)[:160] or type(e).__name__, checked_at=now_cn())

    return await _cached_check(state, "iFinD", IFIND_TTL_S, run)


@router.get("/health", response_model=HealthStatus)
async def health(request: Request) -> HealthStatus:
    st = _state(request)
    names = ("扶摇", "AI", "iFinD")
    outs = await asyncio.gather(_check_fuyao(st), _check_llm(st), _check_ifind(st), return_exceptions=True)
    services: list[ServiceStatus] = []
    for name, out in zip(names, outs):
        if isinstance(out, ServiceStatus):
            services.append(out)
        else:
            services.append(ServiceStatus(name=name, configured=False, ok=False, detail=f"检查失败：{type(out).__name__}", checked_at=now_cn()))
    fuyao = services[0]
    return HealthStatus(
        ok=bool(fuyao.ok), version=st.settings.version, services=services, warm=dict(st.warm),
        demo_faults_enabled=bool(st.settings.demo_faults_enabled), now=now_cn(),
    )


# ───────────────────────────── 目录 / 股票池 ─────────────────────────────


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog() -> CatalogResponse:
    return CatalogResponse(
        metrics=list(catalog.METRICS.values()), ops=list(catalog.OPS), universes=list(catalog.UNIVERSE_PRESETS),
        default_conditions=[c.model_copy(deep=True) for c in catalog.DEFAULT_CONDITIONS],
        disclaimer=catalog.DISCLAIMER, examples=list(catalog.EXAMPLES),
    )


def _preset_matches(q: str) -> list[UniverseOption]:
    text = (q or "").strip().lower()
    if not text:
        return list(catalog.UNIVERSE_PRESETS)
    return [p for p in catalog.UNIVERSE_PRESETS if text in p.name.lower() or (p.code and text in p.code.lower())]


@router.get("/universes/search", response_model=list[UniverseOption])
async def search_universes(request: Request, q: str = Query("", max_length=50)) -> list[UniverseOption]:
    try:
        return await _state(request).repository.search_universes(q)
    except Exception:  # noqa: BLE001 — 检索失败时退回预设匹配
        log.warning("股票池检索失败，退回预设匹配", exc_info=True)
        return _preset_matches(q)


# ───────────────────────────── 解析与校验 ─────────────────────────────


@router.post("/parse", response_model=ParseResult)
async def parse(req: ParseRequest, request: Request) -> ParseResult:
    st = _state(request)
    if len(req.query or "") > 500:
        raise HTTPException(400, "输入过长（最多 500 字），请精简描述")
    llm = st.llm if st.llm is not None and getattr(st.llm, "enabled", False) else None
    searcher = st.repository
    note: str | None = None
    try:
        from ..ai.parser import parse_query  # noqa: PLC0415 — 并行开发中的模块，缺失时降级为规则解析
    except Exception as e:  # noqa: BLE001
        parse_query = None
        note = f"AI 解析模块不可用（{type(e).__name__}），已用规则解析"
    if parse_query is not None:
        try:
            return await parse_query(req, llm, searcher)
        except Exception as e:  # noqa: BLE001
            log.exception("parse_query 异常，降级为规则解析")
            note = f"AI 解析异常（{type(e).__name__}），已用规则解析"
    try:
        res = await fallback.parse(req.query, req.base_strategy, searcher)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise
    if note and not res.llm_error:
        res.llm_error = note
    return res


@router.post("/strategy/validate", response_model=ValidateResult)
async def validate(strategy: Strategy) -> ValidateResult:
    try:
        return validate_strategy(strategy)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise


# ───────────────────────────── 筛选任务 ─────────────────────────────


def _check_faults(st: Any, faults: list[str]) -> None:
    if not faults:
        return
    if not st.settings.demo_faults_enabled:
        raise HTTPException(400, "当前环境未开启故障演示（DEMO_FAULTS_ENABLED=false），不能模拟接口故障")
    unknown = [d for d in faults if d not in catalog.DATASETS]
    if unknown:
        raise HTTPException(400, f"未知的数据集：{'、'.join(unknown)}；可选：{'、'.join(catalog.DATASETS)}")


@router.post("/screen")
async def start_screen(req: ScreenRequest, request: Request) -> dict[str, str]:
    st = _state(request)
    _check_faults(st, req.simulate_failures)
    return {"run_id": st.runs.start(req)}


@router.get("/screen/{run_id}", response_model=ScreenResult)
async def get_screen(run_id: str, request: Request, lite: int = 0) -> ScreenResult:
    try:
        res: ScreenResult = _state(request).runs.get(run_id)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise
    if lite:
        return res.model_copy(update={
            "included": [], "excluded": [], "missing": [], "condition_stats": [], "funnel": [], "datasets": [],
            "universe": None, "summary": None,
        })
    return res


@router.post("/screen/{run_id}/whatif", response_model=WhatIfResult)
async def whatif(run_id: str, strategy: Strategy, request: Request) -> WhatIfResult:
    try:
        return await _state(request).runs.whatif(run_id, strategy)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise


@router.get("/screen/{run_id}/stocks/{thscode}", response_model=StockDetail)
async def stock_detail(run_id: str, thscode: str, request: Request) -> StockDetail:
    try:
        return await _state(request).runs.stock_detail(run_id, thscode)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise


@router.post("/screen/{run_id}/explain/{thscode}", response_model=Explanation)
async def explain_stock(run_id: str, thscode: str, request: Request) -> Explanation:
    try:
        return await _state(request).runs.explain_stock(run_id, thscode)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise


# ───────────────────────────── 资讯 / 比较 ─────────────────────────────


def _name_for(st: Any, code: str) -> str | None:
    for rec in reversed(list(st.runs._runs.values())):
        snap = rec.snapshot
        if snap is not None and code in snap.stocks:
            return snap.stocks[code].info.name
    return None


@router.get("/stocks/{thscode}/news", response_model=NewsResult)
async def stock_news(thscode: str, request: Request) -> NewsResult:
    st = _state(request)
    code = thscode.strip().upper()
    client = st.ifind
    if client is None:
        return NewsResult(thscode=code, status="disabled", error="未配置 IFIND_MCP_TOKEN，资讯功能未启用")
    try:
        return await asyncio.wait_for(get_stock_news(client, code, _name_for(st, code)), 40.0)
    except asyncio.TimeoutError:
        return NewsResult(thscode=code, status="failed", error="iFinD 资讯接口超时（>40 秒）")
    except Exception as e:  # noqa: BLE001
        return NewsResult(thscode=code, status="failed", error=f"资讯获取异常：{type(e).__name__}")


@router.post("/compare", response_model=CompareResult)
async def compare(req: CompareRequest, request: Request) -> CompareResult:
    try:
        return await _state(request).runs.compare(req)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise


# ───────────────────────────── 回测 ─────────────────────────────


@router.post("/backtest")
async def start_backtest(req: BacktestRequest, request: Request) -> dict[str, str]:
    try:
        datetime.strptime(req.start, "%Y-%m-%d")
        if req.end:
            datetime.strptime(req.end, "%Y-%m-%d")
    except ValueError as e:
        raise HTTPException(400, "回测起止日期格式应为 yyyy-MM-dd") from e
    if req.end and req.end < req.start:
        raise HTTPException(400, "回测结束日期不能早于开始日期")
    from datetime import date as _date, timedelta as _td

    from ..data.periods import today_cn

    today = today_cn()
    earliest = _date(today.year - 4, 1, 1) + _td(days=420)  # 日K取数窗口起点 + 约 250 个交易日预热
    if req.start > today.isoformat():
        raise HTTPException(400, f"回测开始日期不能晚于今天（{today.isoformat()}）")
    if req.start < earliest.isoformat():
        raise HTTPException(
            400,
            f"回测开始日期最早为 {earliest.isoformat()}：日K取数窗口从 {today.year - 4}-01-01 开始，"
            "价格类条件需要约 250 个交易日预热，更早的区间会因缺数而失真。",
        )
    return {"backtest_id": _state(request).backtests.start(req)}


@router.get("/backtest/{backtest_id}", response_model=BacktestResult)
async def get_backtest(backtest_id: str, request: Request) -> BacktestResult:
    try:
        return _state(request).backtests.get(backtest_id)
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise


# ───────────────────────────── 保存的策略 ─────────────────────────────


@router.get("/strategies", response_model=list[SavedStrategy])
async def list_strategies(request: Request) -> list[SavedStrategy]:
    return _state(request).store.list_strategies()


@router.post("/strategies", response_model=SavedStrategy)
async def create_strategy(item: SavedStrategy, request: Request) -> SavedStrategy:
    if not (item.name or "").strip():
        raise HTTPException(400, "策略名称不能为空")
    vr = validate_strategy(item.strategy)
    return _state(request).store.save_strategy(item.model_copy(update={"strategy": vr.strategy, "name": item.name.strip()}))


@router.get("/strategies/{sid}", response_model=SavedStrategy)
async def get_strategy(sid: str, request: Request) -> SavedStrategy:
    item = _state(request).store.get_strategy(sid)
    if item is None:
        raise HTTPException(404, "保存的策略不存在")
    return item


@router.delete("/strategies/{sid}")
async def delete_strategy(sid: str, request: Request) -> dict[str, Any]:
    if not _state(request).store.delete_strategy(sid):
        raise HTTPException(404, "保存的策略不存在")
    return {"ok": True, "id": sid}


# ───────────────────────────── 监控 ─────────────────────────────


@router.get("/monitors", response_model=list[Monitor])
async def list_monitors(request: Request) -> list[Monitor]:
    return _state(request).store.list_monitors()


@router.post("/monitors", response_model=Monitor)
async def create_monitor(item: Monitor, request: Request) -> Monitor:
    if not (item.name or "").strip():
        raise HTTPException(400, "监控名称不能为空")
    st = _state(request)
    existing = st.store.get_monitor(item.id) if item.id else None
    vr = validate_strategy(item.strategy)
    if any(c.severity == "error" for c in vr.conflicts):
        raise HTTPException(400, "条件存在冲突，无法创建监控：" + "；".join(c.message for c in vr.conflicts if c.severity == "error"))
    now = now_cn()
    m = item.model_copy(update={
        "name": item.name.strip(),
        "strategy": vr.strategy,
        "created_at": (existing.created_at if existing else None) or item.created_at or now,
        "runs": existing.runs if existing else [],
        "last_run_at": existing.last_run_at if existing else None,
        "next_run_at": item.next_run_at or next_run_at(item.frequency, now),
    })
    return st.store.save_monitor(m)


@router.get("/monitors/{mid}", response_model=Monitor)
async def get_monitor(mid: str, request: Request) -> Monitor:
    m = _state(request).store.get_monitor(mid)
    if m is None:
        raise HTTPException(404, "监控任务不存在")
    return m


@router.delete("/monitors/{mid}")
async def delete_monitor(mid: str, request: Request) -> dict[str, Any]:
    if not _state(request).store.delete_monitor(mid):
        raise HTTPException(404, "监控任务不存在")
    return {"ok": True, "id": mid}


@router.post("/monitors/{mid}/run")
async def run_monitor(mid: str, request: Request) -> dict[str, str]:
    try:
        return {"run_id": _state(request).monitor.run_now(mid)}
    except Exception as e:  # noqa: BLE001
        _raise(e)
        raise


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"], include_in_schema=False)
async def api_not_found(rest: str) -> None:
    raise HTTPException(404, f"接口不存在：/api/{rest}")
