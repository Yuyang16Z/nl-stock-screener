"""领域数据访问：股票池、行业映射、批量取数、数据集状态与特征快照（FeatureSnapshot）。

按依赖取数：策略启用条件的指标 ∪ 展示指标 → catalog.METRICS[m].datasets → 只取需要的数据集。
每个数据集汇总一个 DatasetStatus（部分失败 partial、全部失败 failed、未请求 skipped），失败绝不被当作正常。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from ..catalog import INDUSTRY_LEVEL2_PREFIX
from ..catalog import DATASETS, DISPLAY_METRICS, METRICS, UNIVERSE_LEXICON, UNIVERSE_PRESETS, datasets_for
from ..config import Settings
from ..schemas import (
    DatasetStatus,
    FeatureSnapshot,
    MetricValue,
    ResolvedUniverse,
    SourceRef,
    StockFeatures,
    StockInfo,
    Strategy,
    TickerRow,
    Universe,
    UniverseOption,
)
from .features import (
    REFERENCE_FIELDS,
    Fetched,
    StockRaw,
    compute_stock_metrics,
    latest_bar_date,
    market_trade_date,
    reference_value,
)
from .fuyao import BATCH_SIZE, DATASET_TTL, FuyaoClient, FuyaoError, FuyaoResponse, is_no_dividend_events
from .periods import CN_TZ, history_window, make_report_period, ms_to_datetime, now_cn, parse_date, today_cn

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

# ── 接口 ──
EP_TICKERS = "/api/meta/tickers/list"
EP_CONSTITUENTS = "/api/a-share-index/constituents/ths-stock-list"
EP_INDEX_LIST = "/api/a-share-index/catalog/ths-index-list"
EP_SNAPSHOT = "/api/a-share/prices/snapshot"
EP_VALUATIONS = "/api/a-share/valuations/snapshot"
EP_INDICATORS = "/api/a-share/financials/indicators"
EP_INCOME = "/api/a-share/financials/income-statements"
EP_HISTORY = "/api/a-share/prices/historical"
EP_INDEX_HISTORY = "/api/a-share-index/prices/historical"
EP_DIVIDENDS = "/api/a-share/corporate-actions/adjustment-factors"
EP_CALENDAR = "/api/a-share/calendar/trading-days"

# ── 常量 ──
A_SHARE_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
BENCHMARK_CODE = "000300.SH"
DIVIDEND_LOOKBACK_DAYS = 400
INCOME_LIMIT = 20
TICKER_PAGE_LIMIT = 10000
MAX_TICKER_PAGES = 50
SEARCH_LIMIT = 20
MAX_ERROR_SAMPLES = 5
DELISTING_MARK = "退"
BATCH_DATASETS = ("valuations", "snapshot")
PER_STOCK_DATASETS = ("indicators", "indicators_yoy", "income", "history", "dividends")
RANK_REF_SCOPES = ("industry", "market")

# 行业归属规则（可配置）：成员数 ≥ min_members 的指数中按 prefer 选；都不足时按 fallback 选。
INDUSTRY_PICK_RULE: dict[str, Any] = {"min_members": 8, "prefer": "fewest", "fallback": "most"}

_GROUP_PRESET = {"index": "常用指数", "all_a": "全市场"}
_SUFFIXES = ("概念股", "概念", "板块", "行业", "指数", "股票", "个股", "公司", "股", "类")


def industry_pick_note(rule: Mapping[str, Any] = INDUSTRY_PICK_RULE) -> str:
    return "行业归属：同花顺二级行业（881xxx.TI）成分反查；同业分位以二级行业为参考组。"
    n = int(rule["min_members"])
    prefer = "成员数最少者（最细一级）" if rule.get("prefer", "fewest") == "fewest" else "成员数最多者"
    fallback = "成员数最多者" if rule.get("fallback", "most") == "most" else "成员数最少者"
    return f"行业归属规则：同一股票命中多个同花顺行业指数时，在成员数 ≥ {n} 的指数中取{prefer}；若都不足 {n} 只，取{fallback}。"


def pick_industry(
    candidates: Sequence[tuple[str, str, int]], rule: Mapping[str, Any] = INDUSTRY_PICK_RULE
) -> tuple[str, str] | None:
    """candidates: (行业指数代码, 名称, 成员数)。返回 (industry_code, industry_name)；并列时取代码较小者。"""
    if not candidates:
        return None

    def choose(cands: Sequence[tuple[str, str, int]], how: str) -> tuple[str, str, int]:
        if how == "fewest":
            return min(cands, key=lambda c: (c[2], c[0]))
        return min(cands, key=lambda c: (-c[2], c[0]))

    n = int(rule["min_members"])
    big = [c for c in candidates if c[2] >= n]
    best = choose(big, str(rule.get("prefer", "fewest"))) if big else choose(candidates, str(rule.get("fallback", "most")))
    return best[0], best[1]


# ───────────────────────────── 数据集状态 ─────────────────────────────


def _status_of(requested: int, succeeded: int, failed: int) -> str:
    if requested == 0:
        return "skipped"
    if failed == 0:
        return "ok"
    if succeeded == 0:
        return "failed"
    return "partial"


def _max_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


class _StatusAcc:
    def __init__(self, dataset: str, note: str | None = None) -> None:
        self.dataset = dataset
        self.note = note
        self.requested = 0
        self.succeeded = 0
        self.failed = 0
        self.errors: list[str] = []
        self._as_of_ms: int | None = None
        self._fetched_at: datetime | None = None

    def ok(self, resp: FuyaoResponse | None, n: int = 1) -> None:
        self.requested += n
        self.succeeded += n
        if resp is not None:
            if resp.data_timestamp is not None:
                self._as_of_ms = resp.data_timestamp if self._as_of_ms is None else max(self._as_of_ms, resp.data_timestamp)
            self._fetched_at = _max_dt(self._fetched_at, resp.fetched_at)

    def fail(self, err: FuyaoError, who: str, n: int = 1) -> None:
        self.requested += n
        self.failed += n
        if len(self.errors) < MAX_ERROR_SAMPLES:
            self.errors.append(f"{who}: {err.brief()}")

    def build(self) -> DatasetStatus:
        label, endpoint = DATASETS.get(self.dataset, (self.dataset, ""))
        return DatasetStatus(
            dataset=self.dataset,
            label=label,
            endpoint=endpoint,
            status=_status_of(self.requested, self.succeeded, self.failed),  # type: ignore[arg-type]
            requested=self.requested,
            succeeded=self.succeeded,
            failed=self.failed,
            as_of=ms_to_datetime(self._as_of_ms) if self._as_of_ms is not None else None,
            fetched_at=self._fetched_at,
            errors=list(self.errors),
            note=self.note,
        )


def skipped_status(dataset: str) -> DatasetStatus:
    return _StatusAcc(dataset).build()


def status_from_fetched(
    dataset: str, codes: Iterable[str], fetched: Mapping[str, Fetched], note: str | None = None
) -> DatasetStatus:
    """按股票计数；同一次失败请求（批量）只记一条错误样例，如“600519.SH 等 100 只: code=...”。

    dividends 数据集的 code=3002 "No adjustment events" 表示该股近期无分红送转事件（features 按 0 处理），计为成功。
    """
    acc = _StatusAcc(dataset, note)
    groups: dict[int, tuple[FuyaoError, list[str]]] = {}
    no_events = 0
    for code in codes:
        f = fetched.get(code)
        if f is None:
            continue
        if f.error is not None and dataset == "dividends" and is_no_dividend_events(f.error):
            acc.ok(None)
            no_events += 1
        elif f.error is not None:
            groups.setdefault(id(f.error), (f.error, []))[1].append(code)
        else:
            acc.ok(f.response)
    for err, cs in groups.values():
        who = cs[0] if len(cs) == 1 else f"{cs[0]} 等 {len(cs)} 只"
        acc.fail(err, who, n=len(cs))
    if no_events:
        extra = f"其中 {no_events} 只数据源无分红事件记录（code=3002，No adjustment events），按无分红（0）处理，计为成功"
        acc.note = f"{acc.note}；{extra}" if acc.note else extra
    return acc.build()


def merge_dataset_status(base: DatasetStatus, new: DatasetStatus) -> DatasetStatus:
    """增量合并：任一方 skipped 取另一方；否则计数相加（累计请求），时点取较大，错误样例合并（最多 5 条）。"""
    if new.status == "skipped" or new == base:  # 相同状态（如内存日缓存命中）不重复计数
        return base
    if base.status == "skipped":
        return new
    requested = base.requested + new.requested
    succeeded = base.succeeded + new.succeeded
    failed = base.failed + new.failed
    notes = list(dict.fromkeys(n for n in (base.note, new.note) if n))
    notes.append("含增量补齐，计数为累计请求")
    return DatasetStatus(
        dataset=new.dataset,
        label=new.label,
        endpoint=new.endpoint,
        status=_status_of(requested, succeeded, failed),  # type: ignore[arg-type]
        requested=requested,
        succeeded=succeeded,
        failed=failed,
        as_of=_max_dt(base.as_of, new.as_of),
        fetched_at=_max_dt(base.fetched_at, new.fetched_at),
        errors=list(dict.fromkeys(base.errors + new.errors))[:MAX_ERROR_SAMPLES],
        note="；".join(notes),
    )


# ───────────────────────────── 内部结构 ─────────────────────────────


@dataclass
class BatchResult:
    dataset: str
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)  # thscode → 记录
    fetched: dict[str, Fetched] = field(default_factory=dict)  # thscode → 该股所在批次的结果
    status: DatasetStatus | None = None


@dataclass
class _TickerLoad:
    rows: dict[str, TickerRow]
    page_of: dict[str, FuyaoResponse]
    pages: list[FuyaoResponse]
    status: DatasetStatus
    error: FuyaoError | None
    day: date


@dataclass
class _IndustryLoad:
    mapping: dict[str, tuple[str, str]]  # thscode → (industry_code, industry_name)
    names: dict[str, str]  # industry_code → name
    sizes: dict[str, int]  # industry_code → 成员数
    status: DatasetStatus
    error: FuyaoError | None
    day: date

    @property
    def complete(self) -> bool:
        return self.error is None and self.status.failed == 0


class _Progress:
    def __init__(self, cb: ProgressFn | None) -> None:
        self._cb = cb
        self.total = 1
        self.done = 0
        self._last = 0.0

    def set_total(self, total: int) -> None:
        self.total = max(1, total, self.done)

    def emit(self, text: str, frac: float | None = None) -> None:
        if self._cb is None:
            return
        f = self.done / self.total if frac is None else frac
        f = min(1.0, max(self._last, f))
        self._last = f
        try:
            self._cb(f, text)
        except Exception:  # 进度回调异常不影响取数
            log.warning("进度回调异常", exc_info=True)

    def advance(self, text: str, n: int = 1) -> None:
        self.done = min(self.total, self.done + n)
        self.emit(text)


async def _capture(aw: Awaitable[FuyaoResponse]) -> FuyaoResponse | FuyaoError:
    try:
        return await aw
    except FuyaoError as e:
        return e


def _unique(seq: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(seq))


def _ok_count(br: BatchResult, codes: Iterable[str]) -> int:
    n = 0
    for c in codes:
        f = br.fetched.get(c)
        if f is not None and f.ok:
            n += 1
    return n


def clean_codes(codes: Iterable[str]) -> list[str]:
    """标准化（去空格、大写）并保留合法 A 股代码（六位数字 + .SH/.SZ/.BJ），去重保序。"""
    return _unique(c for c in (str(x).strip().upper() for x in codes) if A_SHARE_CODE_RE.match(c))


def _ticker_row(item: Mapping[str, Any]) -> TickerRow | None:
    code = str(item.get("thscode") or "").strip().upper()
    if not A_SHARE_CODE_RE.match(code):
        return None

    def s(v: Any) -> str | None:
        return None if v is None or v == "" else str(v)

    try:
        return TickerRow(
            thscode=code,
            ticker=s(item.get("ticker")) or code.split(".")[0],
            name=s(item.get("name")),
            exchange=s(item.get("exchange")),
            list_date=s(item.get("list_date")),
        )
    except ValidationError:
        return None


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _strip_suffix(q: str) -> str:
    for suf in _SUFFIXES:
        if q.endswith(suf) and len(q) > len(suf):
            return q[: -len(suf)]
    return q


def _match_score(q: str, name: str) -> int | None:
    n = _norm_text(name)
    if not q or not n:
        return None
    if n == q:
        return 0
    if n.startswith(q):
        return 1
    if q in n:
        return 2
    if len(n) >= 2 and n in q:
        return 3
    return None


# ───────────────────────────── Repository ─────────────────────────────


class Repository:
    def __init__(self, client: FuyaoClient, settings: Settings, *, today: Callable[[], date] | None = None) -> None:
        self.client = client
        self.settings = settings
        self._today = today or today_cn
        self._tickers: _TickerLoad | None = None
        self._industry: _IndustryLoad | None = None
        self._catalogs: dict[str, tuple[date, list[tuple[str, str]]]] = {}
        self._name_hint: dict[str, str] = {}
        self._tickers_lock = asyncio.Lock()
        self._industry_lock = asyncio.Lock()
        self.last_industry_status: DatasetStatus | None = None

    # ── 原始取数（返回 FuyaoResponse，供 services/回测组装） ──

    async def _get(self, path: str, params: Mapping[str, Any], dataset: str, faults: Collection[str]) -> FuyaoResponse:
        return await self.client.get(path, dict(params), DATASET_TTL.get(dataset, 0), dataset, frozenset(faults))

    async def constituents(self, code: str, *, faults: Collection[str] = frozenset()) -> FuyaoResponse:
        return await self._get(EP_CONSTITUENTS, {"thscode": code.strip().upper()}, "constituents", faults)

    async def ths_index_list(self, tag: str, *, faults: Collection[str] = frozenset(), dataset: str = "catalog") -> FuyaoResponse:
        return await self._get(EP_INDEX_LIST, {"tag": tag}, dataset, faults)

    async def indicators(
        self, code: str, report: str, *, faults: Collection[str] = frozenset(), dataset: str = "indicators"
    ) -> FuyaoResponse:
        return await self._get(EP_INDICATORS, {"thscode": code, "report": report}, dataset, faults)

    async def income_quarterly(self, code: str, limit: int = INCOME_LIMIT, *, faults: Collection[str] = frozenset()) -> FuyaoResponse:
        return await self._get(EP_INCOME, {"thscode": code, "period": "quarterly", "limit": int(limit)}, "income", faults)

    async def history(
        self, code: str, start: int, end: int, adjust: str = "backward", *, faults: Collection[str] = frozenset()
    ) -> FuyaoResponse:
        params = {"thscode": code, "interval": "1d", "start": int(start), "end": int(end), "adjust": adjust}
        return await self._get(EP_HISTORY, params, "history", faults)

    async def index_history(self, code: str, start: int, end: int, *, faults: Collection[str] = frozenset()) -> FuyaoResponse:
        params = {"thscode": code, "interval": "1d", "start": int(start), "end": int(end)}
        return await self._get(EP_INDEX_HISTORY, params, "benchmark", faults)

    async def dividends(self, code: str, from_date: date | str, *, faults: Collection[str] = frozenset()) -> FuyaoResponse:
        start = from_date.isoformat() if isinstance(from_date, date) else str(from_date)
        return await self._get(EP_DIVIDENDS, {"thscode": code, "from": start}, "dividends", faults)

    async def trading_days(self, *, faults: Collection[str] = frozenset()) -> FuyaoResponse:
        return await self._get(EP_CALENDAR, {}, "calendar", faults)

    async def _last_trading_day(self, faults: Collection[str] = frozenset()) -> date | None:
        """交易日历中不晚于今天的最后一个交易日；失败返回 None。"""
        try:
            resp = await self.trading_days(faults=faults)
        except FuyaoError:
            return None
        today = self._today()
        days = []
        for it in (resp.data or {}).get("item") or []:
            d = parse_date(str(it.get("date") or ""))
            if d is not None and d <= today:
                days.append(d)
        return max(days) if days else None

    # ── 批量（每批 100） ──

    async def _batch(
        self,
        dataset: str,
        codes: Iterable[str],
        faults: Collection[str],
        on_chunk: Callable[[int], None] | None = None,
    ) -> BatchResult:
        path = EP_VALUATIONS if dataset == "valuations" else EP_SNAPSHOT
        valid = clean_codes(codes)
        chunks = [valid[i : i + BATCH_SIZE] for i in range(0, len(valid), BATCH_SIZE)]

        async def one(chunk: list[str]) -> FuyaoResponse | FuyaoError:
            out = await _capture(self._get(path, {"thscodes": ",".join(chunk)}, dataset, faults))
            if on_chunk is not None:
                on_chunk(len(chunk))
            return out

        outs = await asyncio.gather(*(one(ch) for ch in chunks))
        result = BatchResult(dataset=dataset)
        for chunk, out in zip(chunks, outs):
            if isinstance(out, FuyaoError):
                for c in chunk:
                    result.fetched[c] = Fetched(dataset, error=out)
                continue
            rows: dict[str, dict[str, Any]] = {}
            for it in out.items:
                c = str(it.get("thscode") or "").strip().upper()
                if c and c not in rows:
                    rows[c] = it
            for c in chunk:
                row = rows.get(c)
                if row is not None:
                    result.rows[c] = row
                    if row.get("name") and c not in self._name_hint:
                        self._name_hint[c] = str(row["name"])
                result.fetched[c] = Fetched(dataset, response=out, row=row)
        result.status = status_from_fetched(dataset, valid, result.fetched)
        return result

    async def valuations(self, codes: Iterable[str], *, faults: Collection[str] = frozenset()) -> BatchResult:
        return await self._batch("valuations", codes, frozenset(faults))

    async def snapshots(self, codes: Iterable[str], *, faults: Collection[str] = frozenset()) -> BatchResult:
        return await self._batch("snapshot", codes, frozenset(faults))

    # ── 代码表 ──

    async def ticker_table(self, *, faults: Collection[str] = frozenset()) -> dict[str, TickerRow]:
        load = await self._load_tickers(frozenset(faults))
        if load.error is not None:
            raise load.error
        return dict(load.rows)

    async def _load_tickers(self, faults: frozenset[str]) -> _TickerLoad:
        today = self._today()
        use_memo = "tickers" not in faults

        def memo_ok() -> bool:
            m = self._tickers
            return use_memo and m is not None and m.day == today and m.error is None

        if memo_ok():
            assert self._tickers is not None
            return self._tickers
        async with self._tickers_lock:
            if memo_ok():
                assert self._tickers is not None
                return self._tickers
            load = await self._fetch_tickers(faults, today)
            if use_memo and load.error is None:
                self._tickers = load
            return load

    async def _fetch_tickers(self, faults: frozenset[str], today: date) -> _TickerLoad:
        acc = _StatusAcc("tickers")
        rows: dict[str, TickerRow] = {}
        page_of: dict[str, FuyaoResponse] = {}
        pages: list[FuyaoResponse] = []
        error: FuyaoError | None = None
        offset = 0
        for _ in range(MAX_TICKER_PAGES):
            params = {"asset_type": "a-share", "limit": TICKER_PAGE_LIMIT, "offset": offset}
            try:
                resp = await self._get(EP_TICKERS, params, "tickers", faults)
            except FuyaoError as e:
                acc.fail(e, f"offset={offset}")
                error = e
                break
            acc.ok(resp)
            pages.append(resp)
            items = resp.items
            for it in items:
                row = _ticker_row(it)
                if row is not None and row.thscode not in rows:
                    rows[row.thscode] = row
                    page_of[row.thscode] = resp
            if len(items) < TICKER_PAGE_LIMIT:
                break
            offset += TICKER_PAGE_LIMIT
        acc.note = f"共 {len(rows)} 只A股，分页请求 {acc.requested} 次" + ("；分页未取全" if error is not None and rows else "")
        return _TickerLoad(rows=rows, page_of=page_of, pages=pages, status=acc.build(), error=error, day=today)

    @staticmethod
    def _all_a_codes(tick: _TickerLoad | None) -> list[str]:
        if tick is None:
            return []
        return sorted(c for c, r in tick.rows.items() if DELISTING_MARK not in (r.name or ""))

    @staticmethod
    def _ticker_fetched(code: str, tick: _TickerLoad | None) -> Fetched:
        if tick is None:
            return Fetched("tickers")
        row = tick.rows.get(code)
        if row is not None:
            return Fetched("tickers", response=tick.page_of[code], row=row.model_dump())
        if tick.error is not None:
            return Fetched("tickers", error=tick.error)
        if tick.pages:
            return Fetched("tickers", response=tick.pages[0], row=None)
        return Fetched("tickers")

    # ── 行业映射 ──

    async def industry_map(self, *, faults: Collection[str] = frozenset()) -> dict[str, tuple[str, str]]:
        """thscode → (industry_code, industry_name)；同花顺行业指数成分反查，每日缓存。"""
        load = await self._load_industry(frozenset(faults))
        if load.error is not None and not load.mapping:
            raise load.error
        return dict(load.mapping)

    async def _load_industry(self, faults: frozenset[str]) -> _IndustryLoad:
        today = self._today()
        use_memo = "industry_map" not in faults

        def memo_ok() -> bool:
            m = self._industry
            return use_memo and m is not None and m.day == today and m.complete

        if memo_ok():
            assert self._industry is not None
            return self._industry
        async with self._industry_lock:
            if memo_ok():
                assert self._industry is not None
                return self._industry
            load = await self._fetch_industry(faults, today)
            self.last_industry_status = load.status
            if use_memo and load.complete:
                self._industry = load
            return load

    async def _fetch_industry(self, faults: frozenset[str], today: date) -> _IndustryLoad:
        acc = _StatusAcc("industry_map", industry_pick_note() + "（每日缓存）")
        try:
            cat = await self.ths_index_list("industry", faults=faults, dataset="industry_map")
        except FuyaoError as e:
            acc.fail(e, "同花顺行业指数目录")
            return _IndustryLoad({}, {}, {}, acc.build(), e, today)
        acc.ok(cat)
        inds: list[tuple[str, str]] = []
        seen: set[str] = set()
        for it in cat.items:
            code = str(it.get("thscode") or "").strip().upper()
            name = str(it.get("name") or "").strip()
            if code and name and code not in seen:
                seen.add(code)
                inds.append((code, name))
        # 实测目录混有二级（881xxx，约 90 个）与三级（884xxx，约 230 个）；同业组用二级，统计更稳也少 230 次请求
        level2 = [x for x in inds if x[0].startswith(INDUSTRY_LEVEL2_PREFIX)]
        if level2:
            inds = level2
        outs = await asyncio.gather(
            *(_capture(self._get(EP_CONSTITUENTS, {"thscode": c}, "industry_map", faults)) for c, _ in inds)
        )
        membership: dict[str, list[tuple[str, str, int]]] = {}
        names: dict[str, str] = {}
        sizes: dict[str, int] = {}
        first_err: FuyaoError | None = None
        for (code, name), out in zip(inds, outs):
            if isinstance(out, FuyaoError):
                acc.fail(out, f"{code} {name}")
                first_err = first_err or out
                continue
            acc.ok(out)
            members = clean_codes(str(it.get("thscode") or "") for it in out.items)
            names[code] = name
            sizes[code] = len(members)
            for m in members:
                membership.setdefault(m, []).append((code, name, len(members)))
        mapping: dict[str, tuple[str, str]] = {}
        for m, cands in membership.items():
            picked = pick_industry(cands)
            if picked is not None:
                mapping[m] = picked
        error = first_err if not mapping and first_err is not None else None
        return _IndustryLoad(mapping, names, sizes, acc.build(), error, today)

    # ── 股票池 ──

    async def resolve_universe(self, u: Universe, *, faults: Collection[str] = frozenset()) -> ResolvedUniverse:
        resolved, _ = await self._resolve(u, frozenset(faults))
        return resolved

    async def _resolve(self, u: Universe, faults: frozenset[str]) -> tuple[ResolvedUniverse, dict[str, DatasetStatus]]:
        statuses: dict[str, DatasetStatus] = {}
        kind = u.kind
        if kind in ("index", "industry", "concept"):
            code = (u.code or "").strip().upper()
            name = u.name
            matched_note: str | None = None
            if not code and u.query:
                opts = [o for o in await self.search_universes(u.query) if o.code and o.kind in ("index", "industry", "concept")]
                if not opts:
                    raise ValueError(f"未找到与“{u.query}”匹配的指数/行业/概念板块")
                code, name, kind = opts[0].code or "", opts[0].name, opts[0].kind
                matched_note = f"按“{u.query}”匹配到 {name}（{code}）"
            if not code:
                raise ValueError("股票池缺少指数/板块代码")
            acc = _StatusAcc("constituents")
            try:
                resp = await self.constituents(code, faults=faults)
            except FuyaoError as e:
                acc.fail(e, code)
                statuses["constituents"] = acc.build()
                raise
            acc.ok(resp)
            members: list[str] = []
            for it in resp.items:
                c = str(it.get("thscode") or "").strip().upper()
                if A_SHARE_CODE_RE.match(c):
                    members.append(c)
                    if it.get("name") and c not in self._name_hint:
                        self._name_hint[c] = str(it["name"])
            members = sorted(set(members))
            name = name or next((p.name for p in UNIVERSE_PRESETS if p.code == code), None) or code
            acc.note = f"{name}（{code}）：{len(members)} 只成分股"
            statuses["constituents"] = acc.build()
            notes = ["成分股为当前时点（回看历史时存在幸存者偏差）"]
            if matched_note:
                notes.insert(0, matched_note)
            if not members:
                notes.append("成分股列表为空")
            resolved = ResolvedUniverse(
                kind=kind,
                code=code,
                name=name,
                size=len(members),
                members=members,
                source=resp.source(),
                as_of=ms_to_datetime(resp.data_timestamp) if resp.data_timestamp else resp.fetched_at,
                note="；".join(notes),
            )
            return resolved, statuses
        if kind == "all_a":
            tick = await self._load_tickers(faults)
            if tick.error is not None and not tick.rows:
                raise tick.error
            members = self._all_a_codes(tick)
            excluded = len(tick.rows) - len(members)
            note = f"全部A股：代码表 {len(tick.rows)} 只，剔除名称含“{DELISTING_MARK}”的 {excluded} 只"
            if tick.error is not None:
                note += "；代码表分页未取全"
            first = tick.pages[0] if tick.pages else None
            resolved = ResolvedUniverse(
                kind="all_a",
                code=None,
                name=u.name or "全部A股",
                size=len(members),
                members=members,
                source=first.source() if first else None,
                as_of=(ms_to_datetime(first.data_timestamp) if first and first.data_timestamp else (first.fetched_at if first else None)),
                note=note,
            )
            return resolved, statuses
        if kind == "custom":
            raw = [str(c).strip() for c in u.codes if str(c).strip()]
            valid = clean_codes(raw)
            invalid = [c for c in _unique(raw) if not A_SHARE_CODE_RE.match(c.upper())]  # 原样回显用户输入
            tick = await self._load_tickers(faults)
            custom_notes: list[str] = []
            unknown: list[str] = []
            if tick.rows:
                unknown = [c for c in valid if c not in tick.rows]
                valid = [c for c in valid if c in tick.rows]
            else:
                custom_notes.append("A股代码表获取失败，未校验代码是否存在")
            if invalid:
                custom_notes.append(f"已忽略格式非法的代码：{'、'.join(invalid[:10])}")
            if unknown:
                custom_notes.append(f"已忽略代码表中不存在的代码：{'、'.join(unknown[:10])}")
            resolved = ResolvedUniverse(
                kind="custom",
                code=None,
                name=u.name or "自定义股票池",
                size=len(valid),
                members=sorted(valid),
                source=SourceRef(provider="user"),
                note="；".join(custom_notes) or None,
            )
            return resolved, statuses
        raise ValueError(f"不支持的股票池类型：{kind}")

    async def search_universes(self, q: str) -> list[UniverseOption]:
        """预设指数 → 同花顺行业 → 同花顺概念（目录每日缓存），子串匹配，最多 20 个。"""
        text = (q or "").strip()
        if not text:
            return list(UNIVERSE_PRESETS)[:SEARCH_LIMIT]
        norm = _norm_text(text)
        out: list[UniverseOption] = []
        seen: set[tuple[str, str | None]] = set()

        def add(opt: UniverseOption) -> None:
            key = (opt.kind, opt.code)
            if key not in seen:
                seen.add(key)
                out.append(opt)

        for p in UNIVERSE_PRESETS:
            name_hit = norm in _norm_text(p.name) or (p.code is not None and norm in p.code.lower())
            lex_hit = any(
                re.search(pattern, text, flags=re.IGNORECASE) and code == p.code and kind == p.kind
                for pattern, code, _name, kind in UNIVERSE_LEXICON
            )
            if name_hit or lex_hit:
                add(p)

        core = _strip_suffix(norm)
        tags = (("industry", "同花顺行业", "industry"), ("cn_concept", "同花顺概念", "concept"))
        catalogs = await asyncio.gather(*(self._catalog(tag) for tag, _, _ in tags), return_exceptions=True)
        sizes = self._industry.sizes if self._industry is not None else {}
        for (_tag, group, kind), entries in zip(tags, catalogs):
            if isinstance(entries, BaseException):
                if not isinstance(entries, FuyaoError):
                    raise entries
                continue
            scored: list[tuple[int, int, str, str]] = []
            for code, name in entries:
                s = _match_score(core, name)
                if s is None and core != norm:
                    s = _match_score(norm, name)
                if s is not None:
                    scored.append((s, len(name), code, name))
            for _s, _len, code, name in sorted(scored):
                add(UniverseOption(kind=kind, code=code, name=name, group=group, size_hint=sizes.get(code) if kind == "industry" else None))  # type: ignore[arg-type]
        return out[:SEARCH_LIMIT]

    async def _catalog(self, tag: str) -> list[tuple[str, str]]:
        today = self._today()
        memo = self._catalogs.get(tag)
        if memo is not None and memo[0] == today:
            return memo[1]
        resp = await self.ths_index_list(tag)
        entries: list[tuple[str, str]] = []
        for it in resp.items:
            code = str(it.get("thscode") or "").strip().upper()
            name = str(it.get("name") or "").strip()
            if code and name:
                entries.append((code, name))
        self._catalogs[tag] = (today, entries)
        return entries

    # ── 特征快照 ──

    def _stock_info(self, code: str, tick: _TickerLoad | None, ind: _IndustryLoad | None) -> StockInfo:
        row = tick.rows.get(code) if tick is not None else None
        picked = ind.mapping.get(code) if ind is not None else None
        name = (row.name if row is not None and row.name else None) or self._name_hint.get(code)
        return StockInfo(
            thscode=code,
            ticker=row.ticker if row is not None else code.split(".")[0],
            name=name,
            industry=picked[1] if picked else None,
            industry_code=picked[0] if picked else None,
            list_date=row.list_date if row is not None else None,
        )

    async def _fetch_each(
        self,
        dataset: str,
        codes: Sequence[str],
        fn: Callable[[str], Awaitable[FuyaoResponse]],
        prog: _Progress,
    ) -> dict[str, Fetched]:
        label = DATASETS[dataset][0]
        total = len(codes)
        done = 0
        results: dict[str, Fetched] = {}

        async def one(code: str) -> None:
            nonlocal done
            out = await _capture(fn(code))
            results[code] = Fetched(dataset, error=out) if isinstance(out, FuyaoError) else Fetched(dataset, response=out)
            done += 1
            prog.advance(f"取{label} {done}/{total}")

        await asyncio.gather(*(one(c) for c in codes))
        return results

    async def _universe_for(
        self,
        strategy: Strategy,
        members: Sequence[str] | None,
        base: FeatureSnapshot | None,
        faults: frozenset[str],
        statuses: dict[str, DatasetStatus],
    ) -> tuple[ResolvedUniverse, list[str]]:
        if base is not None:
            raw = list(members) if members is not None else list(base.universe.members)
            return base.universe, clean_codes(raw)
        if members is None:
            universe, st = await self._resolve(strategy.universe, faults)
            statuses.update(st)
            return universe, clean_codes(universe.members)
        cleaned = clean_codes(members)
        u = strategy.universe
        universe = ResolvedUniverse(
            kind=u.kind,
            code=u.code,
            name=u.name or "指定股票",
            size=len(cleaned),
            members=cleaned,
            note="由调用方指定成员",
        )
        return universe, cleaned

    async def build_snapshot(
        self,
        strategy: Strategy,
        *,
        faults: Collection[str] = frozenset(),
        progress: ProgressFn | None = None,
        metrics: Collection[str] | None = None,
        members: Sequence[str] | None = None,
        base: FeatureSnapshot | None = None,
    ) -> FeatureSnapshot:
        """按策略依赖取数并计算特征。

        metrics=None → 策略启用条件指标 ∪ DISPLAY_METRICS；members=None → 股票池成分；
        base 非空 → 在 base 上增量补齐（只为 members 补缺少的指标，保留 base 数据并合并数据集状态）。
        """
        fset = frozenset(faults or ())
        today = self._today()
        report = make_report_period(strategy.report_period, today)
        if base is not None and base.report_period.report != report.report:
            raise ValueError(f"增量补齐要求报告期一致：基准快照为 {base.report_period.report}，本次为 {report.report}")
        prog = _Progress(progress)
        prog.emit("解析股票池", 0.0)
        statuses: dict[str, DatasetStatus] = {}
        universe, member_list = await self._universe_for(strategy, members, base, fset, statuses)

        # 指标与依赖
        enabled = [c for c in strategy.conditions if c.enabled and c.metric in METRICS]
        want = set(metrics) if metrics is not None else {c.metric for c in enabled} | set(DISPLAY_METRICS)
        want = {m for m in want if m in METRICS}
        need: dict[str, set[str]] = {}
        for code in member_list:
            have = set(base.stocks[code].metrics) if base is not None and code in base.stocks else set()
            if base is None or code not in base.stocks or want - have:
                need[code] = want - have
        new_codes = [c for c in need if base is None or c not in base.stocks]
        ds_codes: dict[str, list[str]] = {ds: [c for c in need if ds in datasets_for(need[c])] for ds in DATASETS}

        ref_conds = [c for c in enabled if c.rank_scope in RANK_REF_SCOPES]
        ref_metrics = sorted({c.metric for c in ref_conds if c.metric in REFERENCE_FIELDS})
        if base is not None:
            ref_metrics = [m for m in ref_metrics if m not in base.reference]
        fill_ref_industry = bool(ref_conds) and (base is None or not base.reference_industry)
        need_tickers = bool(need) or bool(ref_metrics)
        need_industry = bool(new_codes) or fill_ref_industry
        need_bench = bool(ds_codes["benchmark"])

        # 代码表（每日缓存；StockInfo、is_st、listing_days、全A参考组都依赖它）
        tick: _TickerLoad | None = None
        if need_tickers:
            prog.emit("取A股代码表")
            tick = await self._load_tickers(fset)
            statuses["tickers"] = tick.status
        all_codes = self._all_a_codes(tick) if ref_metrics else []
        batch_codes: dict[str, list[str]] = {}
        for ds in BATCH_DATASETS:
            ref_for_ds = any(REFERENCE_FIELDS[m][0] == ds for m in ref_metrics)
            batch_codes[ds] = _unique(ds_codes[ds] + (all_codes if ref_for_ds else []))

        industry_units = 1 if (self._industry is not None and self._industry.day == today) else 30
        compute_units = max(1, len(need) // 20)
        prog.set_total(
            (1 if need_tickers else 0)
            + (industry_units if need_industry else 0)
            + sum((len(batch_codes[ds]) + BATCH_SIZE - 1) // BATCH_SIZE for ds in BATCH_DATASETS)
            + sum(len(ds_codes[ds]) for ds in PER_STOCK_DATASETS)
            + (1 if need_bench else 0)
            + compute_units
        )
        if need_tickers:
            prog.advance("取A股代码表")

        # 行业归属（每日缓存；失败则行业为空，并在 datasets 里记录 failed）
        ind: _IndustryLoad | None = None
        if need_industry:
            prog.emit("取同花顺行业归属")
            ind = await self._load_industry(fset)
            statuses["industry_map"] = ind.status
            prog.advance("取同花顺行业归属", industry_units)

        # 估值 / 行情快照：批量；需要全A参考组时与股票池合并为一次批量取数
        batch_results: dict[str, BatchResult] = {}
        reference: dict[str, dict[str, float]] = {}
        for ds in BATCH_DATASETS:
            codes = batch_codes[ds]
            if not codes:
                continue
            label = DATASETS[ds][0]
            n_chunks = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE
            counter = [0]

            def on_chunk(_n: int, label: str = label, n_chunks: int = n_chunks, counter: list[int] = counter) -> None:
                counter[0] += 1
                prog.advance(f"取{label} {counter[0]}/{n_chunks} 批")

            br = await self._batch(ds, codes, fset, on_chunk)
            batch_results[ds] = br
            uni = ds_codes[ds]
            ref_for_ds = [m for m in ref_metrics if REFERENCE_FIELDS[m][0] == ds]
            note = None
            if ref_for_ds:
                ok_ref = _ok_count(br, all_codes)
                note = f"含全A参考组：{ok_ref}/{len(all_codes)} 只取数成功（用于行业/全市场分位，参考组为当前全部A股）"
                pool = _unique(all_codes + uni)
                if _ok_count(br, pool) > 0:
                    for m in ref_for_ds:
                        vals: dict[str, float] = {}
                        for c in pool:
                            v = reference_value(m, br.rows.get(c))
                            if v is not None:
                                vals[c] = v
                        reference[m] = vals
            statuses[ds] = status_from_fetched(ds, uni if uni else all_codes, br.fetched, note)

        # 逐股数据集
        start_ms, end_ms = history_window(today)
        div_from = today - timedelta(days=DIVIDEND_LOOKBACK_DAYS)
        fetchers: dict[str, Callable[[str], Awaitable[FuyaoResponse]]] = {
            "indicators": lambda c: self.indicators(c, report.report, faults=fset),
            "indicators_yoy": lambda c: self.indicators(c, report.yoy_report, faults=fset, dataset="indicators_yoy"),
            "income": lambda c: self.income_quarterly(c, INCOME_LIMIT, faults=fset),
            "history": lambda c: self.history(c, start_ms, end_ms, "backward", faults=fset),
            "dividends": lambda c: self.dividends(c, div_from, faults=fset),
        }
        per_stock: dict[str, dict[str, Fetched]] = {}
        for ds in PER_STOCK_DATASETS:
            codes = ds_codes[ds]
            if not codes:
                continue
            per_stock[ds] = await self._fetch_each(ds, codes, fetchers[ds], prog)
            statuses[ds] = status_from_fetched(ds, codes, per_stock[ds])

        # 基准指数
        bench: Fetched | None = None
        if need_bench:
            prog.emit("取基准指数日K（沪深300）")
            acc = _StatusAcc("benchmark")
            out = await _capture(self.index_history(BENCHMARK_CODE, start_ms, end_ms, faults=fset))
            if isinstance(out, FuyaoError):
                bench = Fetched("benchmark", error=out)
                acc.fail(out, BENCHMARK_CODE)
            else:
                bench = Fetched("benchmark", response=out)
                acc.ok(out)
            statuses["benchmark"] = acc.build()
            prog.advance("取基准指数日K（沪深300）")

        # 价格窗口截止交易日：个股最近日K日期的众数；无个股日K时用基准；增量时与 base 取较晚者
        trade_date = market_trade_date(latest_bar_date(f) for f in per_stock.get("history", {}).values())
        if trade_date is None:
            trade_date = latest_bar_date(bench)
        if trade_date is None:
            trade_date = await self._last_trading_day(fset)
        # 快照/估值信封 timestamp 是服务端就绪时间（实测周日也显示当前时刻），数据集时点改用最近交易日收盘
        if trade_date is not None:
            close_dt = datetime.combine(trade_date, datetime.min.time(), tzinfo=CN_TZ).replace(hour=15)
            for ds in ("snapshot", "valuations"):
                stt = statuses.get(ds)
                if stt is not None and stt.status != "skipped":
                    api_t = f"{stt.as_of:%Y-%m-%d %H:%M}" if stt.as_of else "无"
                    statuses[ds] = stt.model_copy(update={
                        "as_of": close_dt,
                        "note": ((stt.note + "；") if stt.note else "") + f"数据时点取最近交易日 {trade_date.isoformat()} 收盘；接口返回时间 {api_t}",
                    })
        base_day = parse_date(base.trade_date) if base is not None and base.trade_date else None
        if base_day is not None:
            trade_date = base_day if trade_date is None else max(trade_date, base_day)

        # 计算
        prog.emit("计算指标")
        infos: dict[str, StockInfo] = {}
        new_metrics: dict[str, dict[str, MetricValue]] = {}
        for code, ms in need.items():
            info = base.stocks[code].info if base is not None and code in base.stocks else self._stock_info(code, tick, ind)
            infos[code] = info
            datasets: dict[str, Fetched] = {}
            for ds in datasets_for(ms):
                f: Fetched | None = None
                if ds == "tickers":
                    f = self._ticker_fetched(code, tick)
                elif ds in batch_results:
                    f = batch_results[ds].fetched.get(code)
                elif ds in per_stock:
                    f = per_stock[ds].get(code)
                if f is not None:
                    datasets[ds] = f
            raw = StockRaw(info=info, datasets=datasets)
            new_metrics[code] = compute_stock_metrics(raw, ms, report, today, benchmark=bench, trade_date=trade_date)
        prog.advance("计算指标", compute_units)

        # 合并
        stocks: dict[str, StockFeatures] = dict(base.stocks) if base is not None else {}
        for code in need:
            prev = stocks.get(code)
            stocks[code] = StockFeatures(info=infos[code], metrics={**(prev.metrics if prev else {}), **new_metrics[code]})

        new_statuses = [statuses.get(ds) or skipped_status(ds) for ds in DATASETS]
        if base is not None:
            base_map = {s.dataset: s for s in base.datasets}
            datasets_out = [merge_dataset_status(base_map.get(s.dataset) or skipped_status(s.dataset), s) for s in new_statuses]
        else:
            datasets_out = new_statuses

        ref_industry = dict(base.reference_industry) if base is not None else {}
        industry_names = dict(base.industry_names) if base is not None else {}
        if fill_ref_industry and ind is not None and ind.mapping:
            ref_industry = {c: v[0] for c, v in sorted(ind.mapping.items())}
            industry_names = {**industry_names, **{v[0]: v[1] for v in ind.mapping.values()}}

        snap = FeatureSnapshot(
            snapshot_id=uuid.uuid4().hex[:16],
            universe=universe,
            stocks=stocks,
            reference={**(base.reference if base is not None else {}), **reference},
            reference_industry=ref_industry,
            industry_names=industry_names,
            datasets=datasets_out,
            report_period=report,
            trade_date=trade_date.isoformat() if trade_date is not None else None,
            metrics_available=sorted(set(base.metrics_available if base is not None else []) | want),
            prefiltered=base.prefiltered if base is not None else False,
            created_at=now_cn(),
        )
        prog.emit("完成", 1.0)
        return snap
