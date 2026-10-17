from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from app.data.cache import ApiCache, make_cache_key
from app.data.fuyao import (
    FAULT_PREFIX,
    NO_KEY_MESSAGE,
    RETRY_LIMITS,
    FaultInjector,
    FuyaoClient,
    FuyaoError,
    TokenBucket,
    parse_envelope,
)

try:
    from _fixtures_data import BASE_URL, FIXED_NOW, envelope, make_settings
except ImportError:  # pragma: no cover - 以包方式导入时
    from tests._fixtures_data import BASE_URL, FIXED_NOW, envelope, make_settings

PATH = "/api/a-share/valuations/snapshot"
URL = BASE_URL + PATH
PARAMS = {"thscodes": "600001.SH,000007.SZ"}
OK_DATA = {
    "timestamp": 1784736000000,
    "total": 2,
    "item": [
        {"thscode": "600001.SH", "ticker": "600001", "name": "测试甲", "pe_ttm": 15.5, "pb_mrq": 1.2},
        {"thscode": "000007.SZ", "ticker": "000007", "name": "测试庚", "pe_ttm": None, "pb_mrq": 2.0},
    ],
}


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_client(
    *,
    cache: ApiCache | None = None,
    backoff: float = 0.0,
    fault_injector: FaultInjector | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **settings: Any,
) -> tuple[FuyaoClient, SleepRecorder]:
    sleeper = SleepRecorder()
    client = FuyaoClient(
        make_settings(**settings),
        cache,
        fault_injector,
        transport=transport,
        sleep=sleeper,
        backoff_base_s=backoff,
    )
    return client, sleeper


def ok_response(data: Any = OK_DATA, rid: str = "rid-ok") -> httpx.Response:
    return httpx.Response(200, json=envelope(data, request_id=rid))


def err_response(code: int, rid: str = "rid-err", message: str = "错误") -> httpx.Response:
    return httpx.Response(200, json=envelope(None, code=code, message=message, request_id=rid))


async def test_success_parses_envelope_and_records_source() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=ok_response(rid="rid-1"))
        client, _ = make_client()
        resp = await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert route.call_count == 1
    req = route.calls.last.request
    assert req.headers["X-api-key"] == "test-key"
    assert req.url.params["thscodes"] == "600001.SH,000007.SZ"
    assert resp.request_id == "rid-1"
    assert resp.data_timestamp == 1784736000000
    assert resp.cache_hit is False
    assert resp.dataset == "valuations"
    assert [it["thscode"] for it in resp.items] == ["600001.SH", "000007.SZ"]
    src = resp.source("pe_ttm", thscode="600001.SH")
    assert src.provider == "fuyao" and src.endpoint == PATH and src.field == "pe_ttm"
    assert src.request_id == "rid-1" and src.data_timestamp == 1784736000000
    assert src.params == {"thscodes": "600001.SH", "batch_size": 2}  # 批量来源只保留本股
    assert src.fetched_at is not None


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (1001, "bad_request"),
        (1002, "bad_request"),
        (1003, "bad_request"),
        (1004, "bad_request"),
        (2001, "auth"),
        (2003, "permission"),
        (3001, "not_found"),
        (3002, "not_ready"),
        (3004, "unsupported"),
        (4001, "rate_limited"),
        (5001, "internal"),
        (5002, "upstream_timeout"),
        (5003, "upstream_unavailable"),
    ],
)
async def test_error_code_mapping_and_retry_counts(code: int, kind: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=err_response(code, rid=f"rid-{code}"))
        client, sleeper = make_client()
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    e = ei.value
    assert e.kind == kind
    assert e.code == code
    assert e.request_id == f"rid-{code}"
    assert e.endpoint == PATH and e.dataset == "valuations"
    assert f"code={code}" in e.brief() and f"request_id=rid-{code}" in e.brief()
    assert route.call_count == 1 + RETRY_LIMITS.get(kind, 0)
    assert len(sleeper.delays) == RETRY_LIMITS.get(kind, 0)


async def test_http_429_then_success_with_backoff() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(side_effect=[httpx.Response(429, text="Too Many Requests"), ok_response(rid="rid-after")])
        client, sleeper = make_client(backoff=1.0)
        resp = await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert route.call_count == 2
    assert resp.request_id == "rid-after"
    assert len(sleeper.delays) == 1
    assert 1.0 <= sleeper.delays[0] <= 1.25  # 1s + 抖动
    assert client.stats["retries"] == 1


async def test_rate_limited_backoff_1_2_4_then_give_up() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=httpx.Response(429))
        client, sleeper = make_client(backoff=1.0)
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.kind == "rate_limited"
    assert route.call_count == 4  # 首次 + 3 次重试
    for delay, base in zip(sleeper.delays, (1.0, 2.0, 4.0)):
        assert base <= delay <= base * 1.25
    assert len(sleeper.delays) == 3


async def test_5003_retries_exhausted() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=err_response(5003, rid="rid-5003"))
        client, _ = make_client()
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    e = ei.value
    assert route.call_count == 3  # 首次 + 2 次重试
    assert e.kind == "upstream_unavailable" and e.code == 5003
    assert e.attempts == 3
    assert "共尝试 3 次" in e.describe()
    assert "code=5003 上游不可用 (request_id=rid-5003)" in e.describe()


async def test_2001_is_not_retried() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=err_response(2001))
        client, sleeper = make_client()
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.kind == "auth"
    assert route.call_count == 1
    assert sleeper.delays == []


async def test_timeout_maps_to_network_and_retries() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(side_effect=httpx.ReadTimeout("timed out"))
        client, _ = make_client()
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.kind == "network"
    assert "超时" in ei.value.message
    assert ei.value.code is None
    assert route.call_count == 3


async def test_connect_error_then_success() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(side_effect=[httpx.ConnectError("boom"), ok_response()])
        client, _ = make_client()
        resp = await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert route.call_count == 2
    assert resp.items


async def test_non_json_body_is_invalid_response_without_retry() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=httpx.Response(200, text="<html>gateway</html>"))
        client, _ = make_client()
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.kind == "invalid_response"
    assert "JSON" in ei.value.message
    assert route.call_count == 1


async def test_missing_code_field_is_invalid_response() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).mock(return_value=httpx.Response(200, json={"data": {"item": []}}))
        client, _ = make_client()
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.kind == "invalid_response"


async def test_http_502_without_envelope_is_retried() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=httpx.Response(502, text="bad gateway"))
        client, _ = make_client()
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.kind == "upstream_unavailable"
    assert ei.value.http_status == 502
    assert route.call_count == 3


async def test_cache_hit_and_original_fetch_time() -> None:
    cache = ApiCache(":memory:")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=ok_response(rid="rid-cached"))
        client, _ = make_client(cache=cache)
        first = await client.get(PATH, {"thscodes": "600001.SH,000007.SZ", "x": None}, 600, "valuations")
        second = await client.get(PATH, dict(reversed(list(PARAMS.items()))), 600, "valuations")
        await client.aclose()
    assert route.call_count == 1
    assert first.cache_hit is False and second.cache_hit is True
    assert second.request_id == "rid-cached"
    assert second.fetched_at == first.fetched_at  # 命中缓存仍展示真实取数时间
    assert second.data == first.data
    assert second.source("pe_ttm").cache_hit is True
    assert client.stats["cache_hits"] == 1


async def test_zero_ttl_skips_cache() -> None:
    cache = ApiCache(":memory:")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=ok_response())
        client, _ = make_client(cache=cache)
        await client.get(PATH, PARAMS, 0, "valuations")
        await client.get(PATH, PARAMS, 0, "valuations")
        await client.aclose()
    assert route.call_count == 2
    assert len(cache) == 0


async def test_failures_are_not_cached() -> None:
    cache = ApiCache(":memory:")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(side_effect=[err_response(2001), ok_response(rid="rid-2"), ok_response(rid="rid-3")])
        client, _ = make_client(cache=cache)
        with pytest.raises(FuyaoError):
            await client.get(PATH, PARAMS, 600, "valuations")
        assert len(cache) == 0
        ok = await client.get(PATH, PARAMS, 600, "valuations")
        again = await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert route.call_count == 2
    assert ok.request_id == "rid-2" and again.cache_hit and again.request_id == "rid-2"


async def test_fault_injection_skips_network_and_cache() -> None:
    cache = ApiCache(":memory:")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=ok_response(rid="rid-real"))
        client, _ = make_client(cache=cache)
        await client.get(PATH, PARAMS, 600, "valuations")  # 先写入缓存
        assert route.call_count == 1
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations", faults={"valuations"})
        # 不读缓存（仍抛错）、不发请求、不写缓存
        assert route.call_count == 1
        assert len(cache) == 1
        cached = cache.get(make_cache_key(PATH, PARAMS))
        assert cached is not None and cached.request_id == "rid-real"
        # 故障只作用于本次调用
        after = await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    e = ei.value
    assert e.code == 5003 and e.kind == "upstream_unavailable"
    assert e.message.startswith(FAULT_PREFIX)
    assert e.simulated is True
    assert FAULT_PREFIX in e.brief()
    assert after.cache_hit is True
    assert client.stats["faults"] == 1


async def test_fault_injector_in_constructor() -> None:
    with respx.mock(assert_all_called=False) as router:
        hist = router.get(BASE_URL + "/api/a-share/prices/historical").mock(return_value=ok_response({"item": []}))
        router.get(URL).mock(return_value=ok_response())
        client, _ = make_client(fault_injector=FaultInjector({"history"}))
        with pytest.raises(FuyaoError) as ei:
            await client.get("/api/a-share/prices/historical", {"thscode": "600001.SH"}, 60, "history")
        ok = await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.message.startswith(FAULT_PREFIX) and ei.value.code == 5003
    assert hist.call_count == 0
    assert ok.items


async def test_missing_api_key_raises_auth_without_request() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(URL).mock(return_value=ok_response())
        client, _ = make_client(fuyao_api_key=None)
        with pytest.raises(FuyaoError) as ei:
            await client.get(PATH, PARAMS, 600, "valuations")
        await client.aclose()
    assert ei.value.kind == "auth"
    assert ei.value.message == NO_KEY_MESSAGE
    assert route.call_count == 0
    assert client.enabled is False


async def test_semaphore_limits_concurrency() -> None:
    state = {"inflight": 0, "max": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["inflight"] += 1
        state["max"] = max(state["max"], state["inflight"])
        await asyncio.sleep(0.01)
        state["inflight"] -= 1
        return httpx.Response(200, json=envelope({"item": []}))

    client, _ = make_client(transport=httpx.MockTransport(handler), fuyao_max_concurrency=2)
    await asyncio.gather(*(client.get(PATH, {"thscodes": f"60000{i}.SH"}, 0, "valuations") for i in range(6)))
    await client.aclose()
    assert state["max"] == 2
    assert client.stats["requests"] == 6


async def test_token_bucket_spaces_requests() -> None:
    class FakeTime:
        def __init__(self) -> None:
            self.t = 0.0
            self.sleeps: list[float] = []

        def clock(self) -> float:
            return self.t

        async def sleep(self, d: float) -> None:
            self.sleeps.append(d)
            self.t += d

    ft = FakeTime()
    bucket = TokenBucket(2.0, capacity=1.0, clock=ft.clock, sleep=ft.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert sum(ft.sleeps) == pytest.approx(1.0)  # 2 次/秒：第 2、3 次各等 0.5 秒

    unlimited = TokenBucket(0.0, clock=ft.clock, sleep=ft.sleep)
    before = len(ft.sleeps)
    for _ in range(5):
        await unlimited.acquire()
    assert len(ft.sleeps) == before


def test_parse_envelope_edge_cases() -> None:
    common: dict[str, Any] = {"endpoint": PATH, "params": {}, "dataset": "valuations", "fetched_at": FIXED_NOW}
    resp, env = parse_envelope(200, b'{"code":0,"message":"success","request_id":"r","data":null}', **common)
    assert resp.data is None and resp.items == [] and env["request_id"] == "r"
    with pytest.raises(FuyaoError) as ei:
        parse_envelope(429, b"", **common)
    assert ei.value.kind == "rate_limited"
    with pytest.raises(FuyaoError) as ei:
        parse_envelope(200, b'{"code":0,"message":"success"}', **common)
    assert ei.value.kind == "invalid_response"
    with pytest.raises(FuyaoError) as ei:
        parse_envelope(200, b'{"code":"x","data":{}}', **common)
    assert ei.value.kind == "invalid_response"
    with pytest.raises(FuyaoError) as ei:
        parse_envelope(200, b'{"code":5999,"message":"m","request_id":"r9","data":null}', **common)
    assert ei.value.kind == "internal" and ei.value.request_id == "r9"
