"""FastAPI 应用入口：依赖装配（lifespan）、启动预热、监控调度、静态前端（SPA 回退）。

无 .env 也能启动：未配置的数据源在 /api/health 中显示为未配置，筛选运行会以明确的中文错误结束。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import catalog
from .ai import fallback
from .ai.llm import LLMClient
from .api.routes import router
from .config import Settings, get_settings
from .data.cache import ApiCache
from .data.fuyao import FuyaoClient
from .data.ifind import IfindClient
from .data.repository import Repository
from .schemas import Universe
from .services.backtests import BacktestManager
from .services.monitor import MonitorScheduler
from .services.runs import RunManager, error_text
from .services.store import Store

log = logging.getLogger("app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
WARM_DELAY_S = 5.0
WARM_QUERY = "经营改善、估值合理、走势相对稳定"
_DEFAULT: Any = object()


async def warm_up(app: FastAPI, delay: float = WARM_DELAY_S) -> None:
    """启动后延迟预热：用题面示例的规则解析策略为预设指数建快照，写入 RunManager 快照缓存。异常只记录不崩溃。"""
    st = app.state
    codes = [c.strip().upper() for c in (st.settings.warm_universes or "").split(",") if c.strip()]
    if not codes:
        return
    if not st.settings.fuyao_enabled:
        for c in codes:
            st.warm[c] = "skipped: 未配置扶摇 API Key"
        return
    for c in codes:
        st.warm[c] = "pending"
    await asyncio.sleep(delay)
    try:
        parsed = await fallback.parse(WARM_QUERY, None, None)
        base = parsed.strategy
    except Exception as e:  # noqa: BLE001
        log.warning("预热策略解析失败", exc_info=True)
        for c in codes:
            st.warm[c] = f"failed: 预热策略解析失败（{type(e).__name__}）"
        return
    if base is None:
        for c in codes:
            st.warm[c] = "failed: 预热策略为空"
        return
    names = {p.code: p.name for p in catalog.UNIVERSE_PRESETS if p.code}
    for code in codes:
        strategy = base.model_copy(update={"universe": Universe(kind="index", code=code, name=names.get(code, code))}, deep=True)

        def progress(frac: float, _text: str, code: str = code) -> None:
            st.warm[code] = f"warming {int(max(0.0, min(1.0, frac)) * 100)}%"

        try:
            st.warm[code] = "warming 0%"
            await st.runs.prime(strategy, progress=progress)
            st.warm[code] = "ready"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("预热 %s 失败", code, exc_info=True)
            st.warm[code] = f"failed: {error_text(e)[:120]}"


def create_app(
    settings: Settings | None = None,
    *,
    repository: Any = _DEFAULT,
    llm: Any = _DEFAULT,
    ifind: Any = _DEFAULT,
    background: bool = True,
) -> FastAPI:
    """repository / llm / ifind 可注入（测试用假实现；llm=None 表示不使用大模型）。background=False 时不启动预热与监控循环。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        s = settings or get_settings()
        data_dir = Path(s.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        cache: ApiCache | None = None
        client: FuyaoClient | None = None
        if repository is _DEFAULT:
            cache = ApiCache(str(data_dir / "cache.sqlite"))
            client = FuyaoClient(s, cache)
            repo = Repository(client, s)
        else:
            repo = repository
        llm_client = LLMClient(s) if llm is _DEFAULT else llm
        ifind_client = IfindClient(s) if ifind is _DEFAULT else ifind
        store = Store(str(data_dir / "app.sqlite"))
        runs = RunManager(s, repo, llm_client)
        backtests = BacktestManager(s, repo)
        monitor = MonitorScheduler(store, runs, s)

        st = app.state
        st.settings, st.repository, st.llm, st.ifind = s, repo, llm_client, ifind_client
        st.store, st.runs, st.backtests, st.monitor = store, runs, backtests, monitor
        st.cache, st.fuyao = cache, client
        st.warm = {}
        st.health_cache = {}
        warm_task: asyncio.Task[Any] | None = None
        if background:
            monitor.start()
            warm_task = asyncio.create_task(warm_up(app))
        try:
            yield
        finally:
            if warm_task is not None:
                warm_task.cancel()
                try:
                    await warm_task
                except BaseException:  # noqa: BLE001
                    pass
            await monitor.stop()
            await runs.shutdown()
            await backtests.shutdown()
            for closer in (getattr(client, "aclose", None), getattr(ifind_client, "aclose", None)):
                if closer is not None:
                    try:
                        await closer()
                    except Exception:  # noqa: BLE001
                        pass
            store.close()
            if cache is not None:
                cache.close()

    app = FastAPI(title="ScreenLens 自然语言智能选股与策略解释器", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("未处理的异常")
        return JSONResponse(status_code=500, content={"detail": f"服务器内部错误：{type(exc).__name__}"})

    app.include_router(router)

    if (STATIC_DIR / "index.html").is_file():
        assets = STATIC_DIR / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")
        root = STATIC_DIR.resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> FileResponse:
            if full_path == "api" or full_path.startswith("api/"):
                raise HTTPException(404, "接口不存在")
            if full_path:
                candidate = (root / full_path).resolve()
                if candidate.is_file() and root in candidate.parents:
                    return FileResponse(str(candidate))
            return FileResponse(str(root / "index.html"))

    return app


app = create_app()
