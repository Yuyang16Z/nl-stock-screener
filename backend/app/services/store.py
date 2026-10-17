"""SQLite 持久化：保存的策略、监控任务（含运行历史 JSON）。线程锁保护单连接，接口全部同步、调用开销极小。"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from ..data.periods import now_cn
from ..schemas import Monitor, MonitorRun, SavedStrategy

MAX_MONITOR_RUNS = 30  # 每个监控保留最近 N 次运行记录


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS strategies (id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS monitors (id TEXT PRIMARY KEY, body TEXT NOT NULL, runs TEXT NOT NULL DEFAULT '[]')"
            )
            self._conn.commit()

    # ── 策略 ──

    def list_strategies(self) -> list[SavedStrategy]:
        with self._lock:
            rows = self._conn.execute("SELECT body FROM strategies").fetchall()
        items = [SavedStrategy.model_validate_json(r[0]) for r in rows]
        items.sort(key=lambda s: s.updated_at or s.created_at or now_cn(), reverse=True)
        return items

    def get_strategy(self, sid: str) -> SavedStrategy | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM strategies WHERE id = ?", (sid,)).fetchone()
        return SavedStrategy.model_validate_json(row[0]) if row else None

    def save_strategy(self, s: SavedStrategy) -> SavedStrategy:
        now = now_cn()
        existing = self.get_strategy(s.id) if s.id else None
        item = s.model_copy(deep=True)
        if not item.id:
            item.id = new_id()
        item.created_at = (existing.created_at if existing else None) or item.created_at or now
        item.updated_at = now
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO strategies (id, body, updated_at) VALUES (?, ?, ?)",
                (item.id, item.model_dump_json(), now.isoformat()),
            )
            self._conn.commit()
        return item

    def delete_strategy(self, sid: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM strategies WHERE id = ?", (sid,))
            self._conn.commit()
        return cur.rowcount > 0

    # ── 监控 ──

    def _monitor_from_row(self, body: str, runs: str) -> Monitor:
        m = Monitor.model_validate_json(body)
        try:
            m.runs = [MonitorRun.model_validate(r) for r in json.loads(runs or "[]")]
        except (ValueError, TypeError):
            m.runs = []
        return m

    def list_monitors(self) -> list[Monitor]:
        with self._lock:
            rows = self._conn.execute("SELECT body, runs FROM monitors").fetchall()
        items = [self._monitor_from_row(b, r) for b, r in rows]
        items.sort(key=lambda m: m.created_at or now_cn(), reverse=True)
        return items

    def get_monitor(self, mid: str) -> Monitor | None:
        with self._lock:
            row = self._conn.execute("SELECT body, runs FROM monitors WHERE id = ?", (mid,)).fetchone()
        return self._monitor_from_row(row[0], row[1]) if row else None

    def save_monitor(self, m: Monitor) -> Monitor:
        item = m.model_copy(deep=True)
        if not item.id:
            item.id = new_id()
        item.created_at = item.created_at or now_cn()
        runs = item.runs[-MAX_MONITOR_RUNS:]
        item.runs = runs
        body = item.model_copy(update={"runs": []}).model_dump_json()
        runs_json = json.dumps([r.model_dump(mode="json") for r in runs], ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO monitors (id, body, runs) VALUES (?, ?, ?)", (item.id, body, runs_json)
            )
            self._conn.commit()
        return item

    def delete_monitor(self, mid: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM monitors WHERE id = ?", (mid,))
            self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
