from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.data.cache import ApiCache, CachedResponse, make_cache_key

CN = timezone(timedelta(hours=8))


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _resp(rid: str = "rid-1") -> CachedResponse:
    return CachedResponse(
        payload={"code": 0, "message": "success", "request_id": rid, "data": {"timestamp": 1, "item": [{"x": 1.5, "名称": "测试"}]}},
        request_id=rid,
        data_timestamp=1784736000000,
        fetched_at=datetime(2026, 10, 17, 9, 30, tzinfo=CN),
    )


def test_put_get_roundtrip() -> None:
    cache = ApiCache(":memory:")
    key = make_cache_key("/api/a", {"b": 2, "a": "x"})
    cache.put(key, _resp(), 60)
    got = cache.get(key)
    assert got is not None
    assert got.payload["data"]["item"][0] == {"x": 1.5, "名称": "测试"}
    assert got.request_id == "rid-1"
    assert got.data_timestamp == 1784736000000
    assert got.fetched_at == datetime(2026, 10, 17, 9, 30, tzinfo=CN)
    assert len(cache) == 1


def test_expired_entry_returns_none() -> None:
    clock = FakeClock()
    cache = ApiCache(":memory:", clock=clock)
    cache.put("k", _resp(), 60)
    clock.t += 59
    assert cache.get("k") is not None
    clock.t += 2
    assert cache.get("k") is None
    assert cache.purge_expired() == 1
    assert len(cache) == 0


def test_non_positive_ttl_is_not_stored() -> None:
    cache = ApiCache(":memory:")
    cache.put("k", _resp(), 0)
    cache.put("k2", _resp(), -5)
    assert cache.get("k") is None and cache.get("k2") is None
    assert len(cache) == 0


def test_clear_and_overwrite() -> None:
    cache = ApiCache(":memory:")
    cache.put("k", _resp("rid-old"), 60)
    cache.put("k", _resp("rid-new"), 60)
    got = cache.get("k")
    assert got is not None and got.request_id == "rid-new"
    cache.clear()
    assert cache.get("k") is None


def test_cache_key_is_order_independent_and_ignores_none() -> None:
    k1 = make_cache_key("/api/x", {"thscode": "600001.SH", "limit": 20, "period": "quarterly"})
    k2 = make_cache_key("/api/x", {"period": "quarterly", "limit": 20, "thscode": "600001.SH", "start": None})
    assert k1 == k2 == "/api/x?limit=20&period=quarterly&thscode=600001.SH"
    assert make_cache_key("/api/y", {}) == "/api/y"
    assert make_cache_key("/api/y", {"flag": True}) == "/api/y?flag=true"


def test_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "sub" / "cache.sqlite"
    c1 = ApiCache(str(db))
    c1.put("k", _resp(), 3600)
    c1.close()
    c2 = ApiCache(str(db))
    got = c2.get("k")
    assert got is not None and got.request_id == "rid-1"
    c2.close()


def test_corrupted_row_is_dropped() -> None:
    cache = ApiCache(":memory:")
    cache.put("k", _resp(), 60)
    cache._conn.execute("UPDATE api_cache SET payload = ? WHERE key = ?", ("{not json", "k"))
    assert cache.get("k") is None
    assert len(cache) == 0
