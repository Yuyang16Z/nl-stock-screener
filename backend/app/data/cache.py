"""SQLite 响应缓存：key = path + 排序后的 params；过期返回 None。只缓存成功响应（由客户端保证）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError


class CachedResponse(BaseModel):
    payload: dict[str, Any]  # 完整响应信封 {code, message, request_id, data}
    request_id: str | None = None
    data_timestamp: int | None = None
    fetched_at: datetime  # 原始取数时间（命中缓存时仍返回它，界面据此展示真实时点）


def _norm_param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def make_cache_key(path: str, params: Mapping[str, Any] | None = None) -> str:
    """path + 按键排序的 params（忽略 None），与参数传入顺序无关。"""
    items = sorted((str(k), _norm_param(v)) for k, v in (params or {}).items() if v is not None)
    if not items:
        return path
    return path + "?" + "&".join(f"{k}={v}" for k, v in items)


class ApiCache:
    def __init__(self, db_path: str, *, clock: Callable[[], float] = time.time) -> None:
        self.db_path = db_path
        self._clock = clock
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=10)
        with self._lock:
            if db_path != ":memory:":
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    pass
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS api_cache ("
                " key TEXT PRIMARY KEY, payload TEXT NOT NULL, request_id TEXT, data_timestamp INTEGER,"
                " fetched_at TEXT NOT NULL, expires_at REAL NOT NULL)"
            )

    def get(self, key: str) -> CachedResponse | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, request_id, data_timestamp, fetched_at, expires_at FROM api_cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        payload, request_id, data_timestamp, fetched_at, expires_at = row
        if float(expires_at) <= self._clock():
            return None
        try:
            return CachedResponse(
                payload=json.loads(payload),
                request_id=request_id,
                data_timestamp=data_timestamp,
                fetched_at=datetime.fromisoformat(fetched_at),
            )
        except (ValueError, TypeError, ValidationError):
            self.delete(key)  # 损坏的记录直接丢弃
            return None

    def put(self, key: str, resp: CachedResponse, ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        blob = json.dumps(resp.payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO api_cache (key, payload, request_id, data_timestamp, fetched_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, blob, resp.request_id, resp.data_timestamp, resp.fetched_at.isoformat(), self._clock() + ttl_s),
            )

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM api_cache WHERE key = ?", (key,))

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM api_cache")

    def purge_expired(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM api_cache WHERE expires_at <= ?", (self._clock(),))
            return cur.rowcount or 0

    def __len__(self) -> int:
        with self._lock:
            (n,) = self._conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()
        return int(n)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
