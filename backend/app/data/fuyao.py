"""扶摇 REST 客户端：信封解析、错误码映射、重试/限流、缓存、来源记录、故障注入。

约定：业务结果为 HTTP 200 + 信封 {code, message, request_id, data}，code=0 成功；
限流可能是 HTTP 429 或 code=4001。数据层只透传与记录来源，错误一律映射为显式的 FuyaoError，绝不编造数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from ..catalog import DATASETS
from ..config import Settings
from ..schemas import SourceRef
from .cache import ApiCache, CachedResponse, make_cache_key
from .periods import now_cn

log = logging.getLogger(__name__)

FuyaoErrorKind = Literal[
    "auth",
    "permission",
    "bad_request",
    "not_found",
    "not_ready",
    "unsupported",
    "rate_limited",
    "internal",
    "upstream_timeout",
    "upstream_unavailable",
    "network",
    "invalid_response",
]

CODE_KIND: dict[int, FuyaoErrorKind] = {
    1001: "bad_request",
    1002: "bad_request",
    1003: "bad_request",
    1004: "bad_request",
    2001: "auth",
    2003: "permission",
    3001: "not_found",
    3002: "not_ready",
    3004: "unsupported",
    4001: "rate_limited",
    5001: "internal",
    5002: "upstream_timeout",
    5003: "upstream_unavailable",
}

KIND_LABEL: dict[str, str] = {
    "auth": "未认证",
    "permission": "权限不足",
    "bad_request": "参数错误",
    "not_found": "标的不存在",
    "not_ready": "数据未就绪",
    "unsupported": "标的类型不支持",
    "rate_limited": "频率超限",
    "internal": "服务内部错误",
    "upstream_timeout": "上游超时",
    "upstream_unavailable": "上游不可用",
    "network": "网络异常",
    "invalid_response": "响应格式异常",
}

# 故障注入时按 kind 选用的错误码
KIND_DEFAULT_CODE: dict[str, int] = {
    "bad_request": 1003,
    "auth": 2001,
    "permission": 2003,
    "not_found": 3001,
    "not_ready": 3002,
    "unsupported": 3004,
    "rate_limited": 4001,
    "internal": 5001,
    "upstream_timeout": 5002,
    "upstream_unavailable": 5003,
}

# 最多重试次数（不含首次请求）；未列出的 kind 不重试
RETRY_LIMITS: dict[str, int] = {
    "rate_limited": 3,
    "upstream_timeout": 2,
    "upstream_unavailable": 2,
    "network": 2,
    "internal": 2,
}

# 各数据集缓存 TTL（秒）
DATASET_TTL: dict[str, int] = {
    "snapshot": 60,
    "valuations": 600,
    "indicators": 86400,
    "indicators_yoy": 86400,
    "income": 86400,
    "history": 21600,
    "benchmark": 21600,
    "constituents": 86400,
    "industry_map": 86400,
    "catalog": 86400,
    "tickers": 86400,
    "calendar": 86400,
    "dividends": 86400,
}

BATCH_SIZE = 100  # 行情/估值快照每批最多 100 只
FAULT_PREFIX = "【演示故障注入】"
NO_KEY_MESSAGE = "未配置 FUYAO_API_KEY"

SleepFn = Callable[[float], Awaitable[None]]


def dataset_label(dataset: str | None) -> str:
    if dataset and dataset in DATASETS:
        return DATASETS[dataset][0]
    return {"catalog": "同花顺指数目录", "calendar": "交易日历"}.get(dataset or "", dataset or "扶摇接口")


def kind_for_code(code: int) -> FuyaoErrorKind:
    if code in CODE_KIND:
        return CODE_KIND[code]
    fallback: dict[int, FuyaoErrorKind] = {1: "bad_request", 2: "permission", 3: "not_ready", 4: "rate_limited", 5: "internal"}
    return fallback.get(code // 1000, "invalid_response")


def kind_for_http_status(status: int) -> FuyaoErrorKind:
    if status == 429:
        return "rate_limited"
    if status == 401:
        return "auth"
    if status == 403:
        return "permission"
    if status in (502, 503):
        return "upstream_unavailable"
    if status == 504:
        return "upstream_timeout"
    if status == 408:
        return "network"
    if 400 <= status < 500:
        return "bad_request"
    if status >= 500:
        return "internal"
    return "invalid_response"


def narrow_params(params: Mapping[str, Any] | None, thscode: str | None = None) -> dict[str, Any]:
    """批量请求的来源记录只保留本股（避免每条证据都携带 100 个代码），并注明批量规模。"""
    out = dict(params or {})
    batch = out.get("thscodes")
    if thscode and isinstance(batch, str) and "," in batch:
        out["thscodes"] = thscode
        out["batch_size"] = len([c for c in batch.split(",") if c])
    return out


class FuyaoError(Exception):
    """扶摇调用失败：错误码、request_id、kind 都可回溯。"""

    def __init__(
        self,
        message: str,
        *,
        kind: FuyaoErrorKind,
        code: int | None = None,
        request_id: str | None = None,
        endpoint: str = "",
        dataset: str | None = None,
        params: Mapping[str, Any] | None = None,
        http_status: int | None = None,
        simulated: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind: FuyaoErrorKind = kind
        self.code = code
        self.request_id = request_id
        self.endpoint = endpoint
        self.dataset = dataset
        self.params: dict[str, Any] = dict(params or {})
        self.http_status = http_status
        self.simulated = simulated
        self.attempts = 1

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    def brief(self) -> str:
        """形如 code=5003 上游不可用 (request_id=...)；演示故障注入会附加标记。"""
        code = self.code if self.code is not None else "--"
        text = f"code={code} {self.label} (request_id={self.request_id or '--'})"
        return f"{text} {FAULT_PREFIX}" if self.simulated else text

    def describe(self) -> str:
        text = f"{dataset_label(self.dataset)}获取失败：{self.brief()}"
        if self.message and self.message != self.label:
            text += f"，{self.message}"
        if self.attempts > 1:
            text += f"（共尝试 {self.attempts} 次）"
        return text

    def source(self, *, thscode: str | None = None) -> SourceRef:
        return SourceRef(
            provider="fuyao",
            endpoint=self.endpoint or None,
            params=narrow_params(self.params, thscode),
            request_id=self.request_id,
        )

    def __str__(self) -> str:
        return self.describe()


# 分红送转接口对“近期无除权除息事件”的股票返回 code=3002 + "No adjustment events for thscode=..."：
# 这是“无事件”的业务结果，不是取数失败（实测万科、中芯国际等长期不分红的公司均如此）
NO_DIVIDEND_EVENTS_CODE = 3002
NO_DIVIDEND_EVENTS_MARK = "no adjustment events"


def is_no_dividend_events(err: FuyaoError | None) -> bool:
    """dividends 数据集的 3002 且 message 含 "No adjustment events" → 该股无分红送转事件（演示故障注入不算）。"""
    return (
        err is not None
        and err.code == NO_DIVIDEND_EVENTS_CODE
        and not err.simulated
        and err.dataset in (None, "dividends")
        and NO_DIVIDEND_EVENTS_MARK in (err.message or "").lower()
    )


class FuyaoResponse(BaseModel):
    endpoint: str
    params: dict[str, Any] = {}
    data: Any = None
    request_id: str | None = None
    data_timestamp: int | None = None
    fetched_at: datetime
    cache_hit: bool = False
    dataset: str | None = None

    def source(self, field: str | None = None, *, thscode: str | None = None) -> SourceRef:
        return SourceRef(
            provider="fuyao",
            endpoint=self.endpoint,
            params=narrow_params(self.params, thscode),
            field=field,
            request_id=self.request_id,
            data_timestamp=self.data_timestamp,
            fetched_at=self.fetched_at,
            cache_hit=self.cache_hit,
        )

    @property
    def items(self) -> list[dict[str, Any]]:
        """data.item[]（非字典元素丢弃）。"""
        if isinstance(self.data, dict):
            item = self.data.get("item")
            if isinstance(item, list):
                return [x for x in item if isinstance(x, dict)]
        return []


def fault_error(
    dataset: str,
    endpoint: str,
    params: Mapping[str, Any] | None = None,
    kind: FuyaoErrorKind = "upstream_unavailable",
) -> FuyaoError:
    return FuyaoError(
        f"{FAULT_PREFIX}{dataset_label(dataset)}接口模拟不可用（未发出真实请求）",
        kind=kind,
        code=KIND_DEFAULT_CODE.get(kind, 5003),
        endpoint=endpoint,
        dataset=dataset,
        params=params,
        simulated=True,
    )


@dataclass(frozen=True)
class FaultInjector:
    """演示用故障注入：命中的数据集直接抛错，且不读写缓存。"""

    datasets: frozenset[str] = field(default_factory=frozenset)
    kind: FuyaoErrorKind = "upstream_unavailable"

    def __post_init__(self) -> None:
        object.__setattr__(self, "datasets", frozenset(self.datasets))

    def hits(self, dataset: str) -> bool:
        return dataset in self.datasets

    def error(self, dataset: str, endpoint: str, params: Mapping[str, Any] | None = None) -> FuyaoError:
        return fault_error(dataset, endpoint, params, self.kind)


class TokenBucket:
    """简单令牌桶：rate<=0 表示不限速；容量默认 max(1, rate)。"""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: SleepFn | None = None,
    ) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity) if capacity else max(1.0, self.rate)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rate <= 0:
            return
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self.rate)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def parse_envelope(
    status_code: int,
    content: bytes | str,
    *,
    endpoint: str,
    params: Mapping[str, Any],
    dataset: str | None,
    fetched_at: datetime,
) -> tuple[FuyaoResponse, dict[str, Any]]:
    """解析 HTTP 响应为 (FuyaoResponse, 原始信封)；任何非成功情形抛 FuyaoError。"""
    common: dict[str, Any] = {"endpoint": endpoint, "dataset": dataset, "params": params, "http_status": status_code}
    env: Any = None
    try:
        env = json.loads(content) if content else None
    except (ValueError, UnicodeDecodeError):
        env = None
    rid_raw = env.get("request_id") if isinstance(env, dict) else None
    rid = str(rid_raw) if rid_raw not in (None, "") else None

    if status_code == 429:
        code = _int_or_none(env.get("code")) if isinstance(env, dict) else None
        raise FuyaoError("HTTP 429 触发限流", kind="rate_limited", code=code, request_id=rid, **common)
    if not isinstance(env, dict) or "code" not in env:
        if status_code != 200:
            raise FuyaoError(f"HTTP {status_code}", kind=kind_for_http_status(status_code), request_id=rid, **common)
        what = "响应不是合法 JSON" if env is None else "响应缺少 code 字段"
        raise FuyaoError(what, kind="invalid_response", request_id=rid, **common)
    code = _int_or_none(env.get("code"))
    if code is None:
        raise FuyaoError("响应 code 字段不是整数", kind="invalid_response", request_id=rid, **common)
    if code != 0:
        kind = kind_for_code(code)
        message = str(env.get("message") or KIND_LABEL.get(kind, ""))
        raise FuyaoError(message, kind=kind, code=code, request_id=rid, **common)
    if status_code != 200:
        raise FuyaoError(f"HTTP {status_code}", kind=kind_for_http_status(status_code), request_id=rid, **common)
    if "data" not in env:
        raise FuyaoError("响应缺少 data 字段", kind="invalid_response", request_id=rid, **common)
    data = env.get("data")
    ts = _int_or_none(data.get("timestamp")) if isinstance(data, dict) else None
    resp = FuyaoResponse(
        endpoint=endpoint,
        params=dict(params),
        data=data,
        request_id=rid,
        data_timestamp=ts,
        fetched_at=fetched_at,
        cache_hit=False,
        dataset=dataset,
    )
    return resp, env


class FuyaoClient:
    """扶摇 REST 客户端。全局并发信号量 + 令牌桶限速；成功响应按数据集 TTL 缓存。"""

    def __init__(
        self,
        settings: Settings,
        cache: ApiCache | None = None,
        fault_injector: FaultInjector | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn | None = None,
        backoff_base_s: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.fault_injector = fault_injector
        self._transport = transport
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._backoff_base = max(0.0, float(backoff_base_s))
        self._rng = rng or random.Random()
        self._sem = asyncio.Semaphore(max(1, int(settings.fuyao_max_concurrency)))
        self._bucket = TokenBucket(settings.fuyao_rps)
        self._http: httpx.AsyncClient | None = None
        self.stats: dict[str, int] = {"requests": 0, "cache_hits": 0, "retries": 0, "errors": 0, "faults": 0, "coalesced": 0}
        # 同一请求（路径+参数）正在进行时，后来者等待同一结果，避免启动预热与用户运行重复取数
        self._inflight: dict[str, asyncio.Future[FuyaoResponse]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.fuyao_api_key)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.settings.fuyao_base_url.rstrip("/"),
                headers={
                    "X-api-key": self.settings.fuyao_api_key or "",
                    "Accept": "application/json",
                    "User-Agent": f"ScreenLens/{self.settings.version}",
                },
                timeout=httpx.Timeout(self.settings.fuyao_timeout_s),
                transport=self._transport,
            )
        return self._http

    def backoff_delay(self, kind: str, attempt: int) -> float:
        """rate_limited：1s/2s/4s；其他可重试错误：0.5s/1s；均加 0~25% 抖动。attempt 从 0 开始。"""
        base = self._backoff_base * (2.0**attempt) * (1.0 if kind == "rate_limited" else 0.5)
        return base + base * 0.25 * self._rng.random()

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None,
        ttl_s: int,
        dataset: str,
        faults: Collection[str] = frozenset(),
    ) -> FuyaoResponse:
        clean = {str(k): v for k, v in (params or {}).items() if v is not None}
        faults = faults or frozenset()
        injected = self.fault_injector is not None and self.fault_injector.hits(dataset)
        if dataset in faults or injected:
            self.stats["faults"] += 1
            kind: FuyaoErrorKind = "upstream_unavailable"
            if injected and dataset not in faults and self.fault_injector is not None:
                kind = self.fault_injector.kind
            raise fault_error(dataset, path, clean, kind)
        if not self.settings.fuyao_api_key:
            raise FuyaoError(NO_KEY_MESSAGE, kind="auth", endpoint=path, dataset=dataset, params=clean)

        key = make_cache_key(path, clean)
        use_cache = self.cache is not None and ttl_s > 0
        if use_cache:
            hit = self._cache_get(key)
            if hit is not None:
                self.stats["cache_hits"] += 1
                return FuyaoResponse(
                    endpoint=path,
                    params=clean,
                    data=hit.payload.get("data"),
                    request_id=hit.request_id,
                    data_timestamp=hit.data_timestamp,
                    fetched_at=hit.fetched_at,
                    cache_hit=True,
                    dataset=dataset,
                )

        pending = self._inflight.get(key)
        if pending is not None:
            self.stats["coalesced"] += 1
            shared = await asyncio.shield(pending)
            return shared.model_copy(update={"dataset": dataset})
        task = asyncio.ensure_future(self._fetch(path, clean, dataset, key, use_cache, ttl_s))
        self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _fetch(
        self, path: str, clean: dict[str, Any], dataset: str, key: str, use_cache: bool, ttl_s: int
    ) -> FuyaoResponse:
        """真正发请求（含重试与写缓存）；由 get() 负责同请求合并。"""
        attempt = 0
        while True:
            try:
                resp, envelope = await self._send(path, clean, dataset)
                break
            except FuyaoError as err:
                err.attempts = attempt + 1
                limit = RETRY_LIMITS.get(err.kind, 0)
                if err.kind == "upstream_unavailable" and dataset in ("indicators", "indicators_yoy"):
                    limit = min(limit, 1)  # 实测：未披露报告期返回 5003，多次重试无意义
                if attempt >= limit:
                    self.stats["errors"] += 1
                    raise
                delay = self.backoff_delay(err.kind, attempt)
                attempt += 1
                self.stats["retries"] += 1
                log.info("扶摇 %s 第 %d 次重试（%s），等待 %.2fs", path, attempt, err.brief(), delay)
                await self._sleep(delay)

        if use_cache:
            self._cache_put(
                key,
                CachedResponse(
                    payload=envelope,
                    request_id=resp.request_id,
                    data_timestamp=resp.data_timestamp,
                    fetched_at=resp.fetched_at,
                ),
                ttl_s,
            )
        return resp

    async def _send(self, path: str, params: dict[str, Any], dataset: str) -> tuple[FuyaoResponse, dict[str, Any]]:
        async with self._sem:
            await self._bucket.acquire()
            self.stats["requests"] += 1
            try:
                r = await self._client().get(path, params=params)
            except httpx.TimeoutException as e:
                raise FuyaoError(
                    f"请求超时（超过 {self.settings.fuyao_timeout_s:g} 秒未响应）",
                    kind="network",
                    endpoint=path,
                    dataset=dataset,
                    params=params,
                ) from e
            except httpx.HTTPError as e:
                raise FuyaoError(
                    f"网络异常：{type(e).__name__}", kind="network", endpoint=path, dataset=dataset, params=params
                ) from e
        return parse_envelope(r.status_code, r.content, endpoint=path, params=params, dataset=dataset, fetched_at=now_cn())

    def _cache_get(self, key: str) -> CachedResponse | None:
        assert self.cache is not None
        try:
            return self.cache.get(key)
        except sqlite3.Error as e:  # 缓存故障不影响取数
            log.warning("缓存读取失败：%s", e)
            return None

    def _cache_put(self, key: str, resp: CachedResponse, ttl_s: int) -> None:
        assert self.cache is not None
        try:
            self.cache.put(key, resp, ttl_s)
        except sqlite3.Error as e:
            log.warning("缓存写入失败：%s", e)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> FuyaoClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
