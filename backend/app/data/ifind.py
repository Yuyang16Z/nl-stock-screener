"""iFinD MCP 客户端（streamable HTTP，JSON-RPC 2.0）与个股资讯获取。

流程：initialize（若响应头带 Mcp-Session-Id 则后续携带）→ notifications/initialized → tools/list → tools/call。
响应可能是 application/json，也可能是 text/event-stream（多行 data:），两种都解析。
资讯只作背景参考，不参与筛选与打分。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import timedelta, datetime
from typing import Any

import httpx

from ..config import Settings
from ..schemas import NewsItem, NewsResult, SourceRef
from .periods import CN_TZ, now_cn

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "screenlens", "version": "0.1.0"}

# 资讯工具：先按候选名精确匹配（主控用真实 token 探测后在此登记），否则按名称/描述关键词打分挑选
NEWS_LOOKBACK_DAYS = 30
# 实测（2026-10-17）hexin-ifind-ds-mcp 的资讯工具为 search_news（另有 search_notice 公告、search_trending_news 热点）
NEWS_TOOL_CANDIDATES: list[str] = [
    "search_news",
    "get_stock_news",
    "stock_news",
    "search_stock_news",
    "get_news",
    "search_news",
    "news_search",
    "query_news",
    "get_company_news",
    "get_stock_notice",
    "search_notice",
]
# (关键词, 名称命中权重, 描述命中权重)
NEWS_KEYWORD_WEIGHTS: list[tuple[str, int, int]] = [
    ("news", 6, 3),
    ("新闻", 6, 3),
    ("资讯", 6, 3),
    ("公告", 4, 2),
    ("notice", 4, 2),
]
NEWS_KEYWORDS: tuple[str, ...] = tuple(k for k, _, _ in NEWS_KEYWORD_WEIGHTS)
_STOCK_HINTS = ("stock", "股票", "个股", "a股", "公司", "证券")

_CODE_KEYS = {"thscode", "thscodes", "code", "codes", "stockcode", "stockcodes", "seccode", "secucode", "securitycode", "symbol", "symbols", "ticker", "stock", "windcode"}
_KEYWORD_KEYS = {"keyword", "keywords", "q", "name", "stockname", "secname", "securityname", "entity", "company"}
_QUERY_KEYS = {"query", "question", "text", "prompt", "input", "content", "search", "searchtext", "querytext", "searchstring"}
_LIMIT_KEYS = {"limit", "size", "count", "num", "pagesize", "top", "topn", "n", "maxresults", "number", "rows", "maxcount"}

_TITLE_KEYS = ("title", "标题", "headline", "newstitle", "news_title", "subject", "资讯标题", "新闻标题", "公告标题")
_TIME_KEYS = ("time", "publish_time", "publishtime", "pub_time", "pubtime", "date", "datetime", "publish_date", "pubdate", "ctime", "create_time", "createtime", "rtime", "showtime", "发布时间", "时间", "日期")
_SOURCE_KEYS = ("source", "来源", "media", "site", "publisher", "author", "src", "origin", "媒体")
_URL_KEYS = ("url", "link", "href", "网址", "链接", "news_url", "web_url", "share_url", "pc_url")
_SUMMARY_KEYS = ("summary", "摘要", "资讯内容", "公告内容", "abstract", "digest", "desc", "description", "brief", "content", "内容", "正文", "text")
_CONTAINER_KEYS = ("data", "items", "item", "list", "news", "result", "results", "records", "rows", "datas", "articles", "tables")
_EMPTY_HINTS = ("未找到", "无相关", "没有找到", "暂无", "no result", "not found")


class IfindError(Exception):
    def __init__(self, message: str, *, kind: str = "error", code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.code = code

    def __str__(self) -> str:
        return self.message


# ───────────────────────────── 传输层解析 ─────────────────────────────


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    """解析 SSE 文本：同一事件的多行 data: 以换行拼接后按 JSON 解析；非 JSON 事件忽略。"""
    messages: list[dict[str, Any]] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            payload = "\n".join(buf)
            buf.clear()
            try:
                obj = json.loads(payload)
            except ValueError:
                return
            if isinstance(obj, dict):
                messages.append(obj)
            elif isinstance(obj, list):
                messages.extend(x for x in obj if isinstance(x, dict))

    for line in text.splitlines():
        if line == "":
            flush()
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            data = line[5:]
            buf.append(data[1:] if data.startswith(" ") else data)
    flush()
    return messages


def _is_response(msg: Mapping[str, Any]) -> bool:
    return "result" in msg or "error" in msg


def _pick_response(messages: Iterable[Mapping[str, Any]], req_id: Any) -> dict[str, Any] | None:
    fallback: dict[str, Any] | None = None
    for m in messages:
        if not _is_response(m):
            continue
        if req_id is None or m.get("id") == req_id:
            return dict(m)
        fallback = dict(m)
    return fallback


async def _read_sse(lines: AsyncIterator[str], req_id: Any) -> dict[str, Any] | None:
    """流式读取 SSE，收到对应 id 的响应即返回（不等待服务端关闭流）。"""
    buf: list[str] = []
    fallback: dict[str, Any] | None = None
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if buf:
                msgs = parse_sse_events("\n".join(f"data: {b}" for b in buf))
                buf.clear()
                for m in msgs:
                    if _is_response(m):
                        if req_id is None or m.get("id") == req_id:
                            return m
                        fallback = m
            continue
        if line.startswith("data:"):
            data = line[5:]
            buf.append(data[1:] if data.startswith(" ") else data)
    if buf:
        picked = _pick_response(parse_sse_events("\n".join(f"data: {b}" for b in buf)), req_id)
        if picked is not None:
            return picked
    return fallback


def _parse_body(raw: bytes, req_id: Any) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return _pick_response(parse_sse_events(text), req_id)  # 未声明 content-type 的 SSE
    if isinstance(obj, dict):
        return obj if _is_response(obj) else None
    if isinstance(obj, list):
        return _pick_response([x for x in obj if isinstance(x, dict)], req_id)
    return None


def _content_text(result: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for c in result.get("content") or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "\n".join(parts)


# ───────────────────────────── 客户端 ─────────────────────────────


class IfindClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.url = settings.ifind_mcp_url
        self.enabled: bool = bool(settings.ifind_mcp_token)
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._initialized = False
        self._ids = itertools.count(1)
        self._tools: list[dict[str, Any]] | None = None
        self._init_lock = asyncio.Lock()
        self.server_info: dict[str, Any] | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self.settings.ifind_timeout_s), transport=self._transport)
        return self._http

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": self.settings.ifind_mcp_token or "",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, payload: dict[str, Any], *, expect_response: bool = True) -> dict[str, Any] | None:
        if not self.enabled:
            raise IfindError("未配置 IFIND_MCP_TOKEN", kind="disabled")
        req_id = payload.get("id")
        try:
            async with self._client().stream("POST", self.url, json=payload, headers=self._headers()) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    self._session_id = sid
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", errors="replace")[:200]
                    kind = "auth" if r.status_code in (401, 403) else "http"
                    raise IfindError(f"iFinD MCP 返回 HTTP {r.status_code}：{body}", kind=kind, code=r.status_code)
                if not expect_response:
                    await r.aread()
                    return None
                ctype = r.headers.get("content-type", "").lower()
                if "text/event-stream" in ctype:
                    msg = await _read_sse(r.aiter_lines(), req_id)
                else:
                    msg = _parse_body(await r.aread(), req_id)
        except httpx.TimeoutException as e:
            raise IfindError(f"iFinD MCP 请求超时（超过 {self.settings.ifind_timeout_s:g} 秒）", kind="timeout") from e
        except httpx.HTTPError as e:
            raise IfindError(f"iFinD MCP 网络异常：{type(e).__name__}", kind="network") from e
        if msg is None:
            raise IfindError("iFinD MCP 响应中未找到对应的 JSON-RPC 结果", kind="invalid_response")
        err = msg.get("error")
        if err:
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise IfindError(f"iFinD MCP JSON-RPC 错误 {code}：{message}", kind="rpc_error", code=code if isinstance(code, int) else None)
        result = msg.get("result")
        return result if isinstance(result, dict) else {"value": result}

    async def initialize(self) -> dict[str, Any]:
        async with self._init_lock:
            if self._initialized:
                return self.server_info or {}
            params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}
            res = await self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": params})
            self.server_info = res or {}
            try:
                await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_response=False)
            except IfindError as e:  # 通知失败不致命
                log.info("iFinD notifications/initialized 失败：%s", e)
            self._initialized = True
            return self.server_info

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        try:
            res = await self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params})
        except IfindError as e:
            if e.code == 404 and self._session_id:  # 会话过期：重新初始化一次
                self._session_id = None
                self._initialized = False
                await self.initialize()
                res = await self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params})
            else:
                raise
        return res or {}

    async def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tools is not None and not refresh:
            return self._tools
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(10):
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            nxt = res.get("nextCursor")
            cursor = str(nxt) if nxt else None
            if not cursor:
                break
        self._tools = tools
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """返回 result（含 content 列表等）；工具报错（isError）抛 IfindError。"""
        res = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            raise IfindError(f"iFinD 工具 {name} 返回错误：{_content_text(res)[:200]}", kind="tool_error")
        return res

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


# ───────────────────────────── 工具选择与参数推断 ─────────────────────────────


def _news_score(tool: Mapping[str, Any]) -> int:
    name = str(tool.get("name") or "").lower()
    desc = str(tool.get("description") or "").lower()
    score = 0
    for kw, w_name, w_desc in NEWS_KEYWORD_WEIGHTS:
        if kw in name:
            score += w_name
        if kw in desc:
            score += w_desc
    if score and any(h in name or h in desc for h in _STOCK_HINTS):
        score += 1
    return score


def pick_news_tool(tools: Iterable[Mapping[str, Any]], candidates: Sequence[str] | None = None) -> dict[str, Any] | None:
    """先按 NEWS_TOOL_CANDIDATES 精确匹配，再按名称/描述关键词（news、新闻、资讯、公告、notice）打分。"""
    tool_list = [t for t in tools if isinstance(t, Mapping) and t.get("name")]
    by_name = {str(t["name"]): t for t in tool_list}
    for c in NEWS_TOOL_CANDIDATES if candidates is None else candidates:
        if c in by_name:
            return dict(by_name[c])
    best: Mapping[str, Any] | None = None
    best_score = 0
    for t in tool_list:
        s = _news_score(t)
        if s > best_score:
            best, best_score = t, s
    return dict(best) if best is not None else None


def _norm_key(key: str) -> str:
    return re.sub(r"[_\-\s]", "", key.lower())


def _schema_type(spec: Mapping[str, Any]) -> str:
    t = spec.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if not t:
        for alt in spec.get("anyOf") or spec.get("oneOf") or []:
            if isinstance(alt, dict) and alt.get("type") and alt.get("type") != "null":
                return str(alt["type"])
    return str(t or "string")


def build_news_arguments(tool: Mapping[str, Any], thscode: str, name: str | None, limit: int) -> dict[str, Any]:
    """按工具 inputSchema 推断参数：代码类字段填 thscode，名称/关键词填股票名，查询句填“名称 最新新闻”，条数填 limit。"""
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}
    required = set(schema.get("required") or [])
    label = name or thscode
    args: dict[str, Any] = {}
    for key, raw_spec in props.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        nk = _norm_key(str(key))
        typ = _schema_type(spec)
        desc = str(spec.get("description") or "")
        if nk in _LIMIT_KEYS:
            n = int(limit)
            maximum = spec.get("maximum")
            if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
                n = min(n, int(maximum))
            args[key] = str(n) if typ == "string" else n
        elif nk in _CODE_KEYS or nk.endswith("code") or nk.endswith("codes"):
            bare = any(h in desc for h in ("不带后缀", "不含后缀", "6位", "六位"))
            value = thscode.split(".")[0] if bare else thscode
            args[key] = [value] if typ == "array" else value
        elif nk in _QUERY_KEYS:
            args[key] = f"{label} 公司动态 经营 业绩"
        elif nk in _KEYWORD_KEYS:
            args[key] = [label] if typ == "array" else label
        elif nk in ("timestart", "starttime", "startdate", "begindate", "datefrom"):
            # 实测 search_news / search_notice 的 time_start、time_end 为必填（yyyy-MM-dd）
            args[key] = (now_cn().date() - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
        elif nk in ("timeend", "endtime", "enddate", "dateto"):
            args[key] = now_cn().date().isoformat()
        elif key in required:
            if "default" in spec:
                args[key] = spec["default"]
            elif isinstance(spec.get("enum"), list) and spec["enum"]:
                args[key] = spec["enum"][0]
    return args


# ───────────────────────────── 结果解析（尽力而为） ─────────────────────────────


def _lower_map(d: Mapping[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in d.items()}


def _first(d: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        v = d.get(k.lower())
        if v not in (None, "", []):
            return v
    return None


def _fmt_time(v: Any) -> str | None:
    if v is None:
        return None
    num: float | None = None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        num = float(v)
    elif isinstance(v, str) and v.strip().isdigit() and len(v.strip()) in (10, 13):
        num = float(v.strip())
    if num is not None and num > 1e9:
        seconds = num / 1000.0 if num > 1e12 else num
        return datetime.fromtimestamp(seconds, tz=CN_TZ).strftime("%Y-%m-%d %H:%M")
    text = str(v).strip()
    return text[:32] or None


def _item_from_dict(d: Mapping[str, Any]) -> NewsItem | None:
    m = _lower_map(d)
    title = _first(m, _TITLE_KEYS)
    if not isinstance(title, str) or not title.strip():
        return None
    url = _first(m, _URL_KEYS)
    summary = _first(m, _SUMMARY_KEYS)
    source = _first(m, _SOURCE_KEYS)
    return NewsItem(
        title=title.strip()[:200],
        time=_fmt_time(_first(m, _TIME_KEYS)),
        source=str(source).strip()[:60] if source is not None and not isinstance(source, (dict, list)) else None,
        url=url.strip() if isinstance(url, str) and url.strip().startswith("http") else None,
        summary=summary.strip()[:300] if isinstance(summary, str) and summary.strip() else None,
    )


def _columnar_rows(d: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """列式表格（{字段: [值...]}）→ 行列表；要求含标题列。"""
    lists = {k: v for k, v in d.items() if isinstance(v, list)}
    if len(lists) < 2:
        return None
    lengths = {len(v) for v in lists.values()}
    if len(lengths) != 1:
        return None
    lower = {str(k).lower() for k in lists}
    if not any(k.lower() in lower for k in _TITLE_KEYS):
        return None
    n = lengths.pop()
    return [{k: v[i] for k, v in lists.items()} for i in range(n)]


def _items_from_obj(obj: Any, depth: int = 0) -> list[NewsItem]:
    if depth > 6:
        return []
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in ("{", "["):
            try:
                return _items_from_obj(json.loads(s), depth + 1)
            except ValueError:
                return []
        return []
    if isinstance(obj, list):
        out: list[NewsItem] = []
        for x in obj:
            if isinstance(x, dict):
                it = _item_from_dict(x)
                if it is not None:
                    out.append(it)
                else:
                    out.extend(_items_from_obj(x, depth + 1))
            elif isinstance(x, (list, str)):
                out.extend(_items_from_obj(x, depth + 1))
        return out
    if isinstance(obj, dict):
        rows = _columnar_rows(obj)
        if rows is not None:
            return _items_from_obj(rows, depth + 1)
        lower = _lower_map(obj)
        for key in _CONTAINER_KEYS:
            if key in lower and isinstance(lower[key], (list, dict, str)):
                sub = _items_from_obj(lower[key], depth + 1)
                if sub:
                    return sub
        it = _item_from_dict(obj)
        if it is not None:
            return [it]
        for v in obj.values():
            if isinstance(v, (list, dict)):
                sub = _items_from_obj(v, depth + 1)
                if sub:
                    return sub
    return []


def _split_md_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _items_from_markdown(text: str) -> list[NewsItem]:
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    header = [h.lower() for h in _split_md_row(lines[0])]

    def col(keys: Sequence[str]) -> int | None:
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return None

    ti = col(("标题", "title", "新闻", "资讯", "公告"))
    if ti is None:
        return []
    tm, src, url = col(("时间", "日期", "time", "date")), col(("来源", "source", "媒体")), col(("链接", "url", "link", "网址"))
    out: list[NewsItem] = []
    for ln in lines[1:]:
        cells = _split_md_row(ln)
        if all(set(c) <= set("-: ") for c in cells):
            continue
        if ti >= len(cells) or not cells[ti]:
            continue

        def cell(i: int | None) -> str | None:
            return cells[i] if i is not None and i < len(cells) and cells[i] else None

        link = cell(url)
        md_link = re.search(r"\((https?://[^)\s]+)\)", link or "")
        out.append(
            NewsItem(
                title=cells[ti][:200],
                time=cell(tm),
                source=cell(src),
                url=md_link.group(1) if md_link else (link if link and link.startswith("http") else None),
            )
        )
    return out


def _items_from_text(text: str) -> list[NewsItem]:
    items = _items_from_markdown(text)
    if items:
        return items
    out: list[NewsItem] = []
    for ln in text.splitlines():
        m = re.match(r"^\s*(?:\d+[.、)]|[-*•])\s*(.+)$", ln)
        if not m:
            continue
        body = m.group(1).strip()
        url_m = re.search(r"https?://\S+", body)
        url = url_m.group(0).rstrip(")）]") if url_m else None
        title = re.sub(r"https?://\S+", "", body)
        title = re.sub(r"[(（\[]\s*[)）\]]", "", title)  # 去链接后留下的空括号
        title = re.sub(r"[(（\[]\s*$", "", title.strip()).strip().strip("-—:：").strip()
        if title:
            out.append(NewsItem(title=title[:200], url=url))
    if out:
        return out
    s = text.strip()
    if not s or any(h in s.lower() for h in _EMPTY_HINTS):
        return []
    first, _, rest = s.partition("\n")
    return [NewsItem(title=first.strip()[:80], summary=rest.strip()[:300] or None)]


def parse_news_items(result: Mapping[str, Any] | None, limit: int = 8) -> list[NewsItem]:
    items = _parse_news_items_raw(result, limit + 4)
    kept = [it for it in items if not any(h in (it.url or "") for h in NON_NEWS_URL_HINTS)]
    return kept[:limit]


NON_NEWS_URL_HINTS = ("kuaicha", "baike.baidu.com")  # 工商登记、百科类页面，不是资讯


def _parse_news_items_raw(result: Mapping[str, Any] | None, limit: int = 8) -> list[NewsItem]:
    """从 tools/call 结果解析资讯：优先 structuredContent，其次 content[].text（JSON / Markdown 表格 / 列表文本）。"""
    if not result:
        return []
    payloads: list[Any] = []
    if result.get("structuredContent"):
        payloads.append(result["structuredContent"])
    for c in result.get("content") or []:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("text"), str):
            payloads.append(c["text"])
        elif isinstance(c.get("resource"), dict) and isinstance(c["resource"].get("text"), str):
            payloads.append(c["resource"]["text"])
    items: list[NewsItem] = []
    for p in payloads:
        if isinstance(p, str):
            s = p.strip()
            parsed: list[NewsItem]
            if s[:1] in ("{", "["):
                try:
                    parsed = _items_from_obj(json.loads(s))
                except ValueError:
                    parsed = _items_from_text(s)
            else:
                parsed = _items_from_text(s)
        else:
            parsed = _items_from_obj(p)
        if parsed:
            items = parsed
            break
    seen: set[str] = set()
    out: list[NewsItem] = []
    for it in items:
        if it.title not in seen:
            seen.add(it.title)
            out.append(it)
    return out[: max(0, int(limit))]


# ───────────────────────────── 入口 ─────────────────────────────


async def get_stock_news(client: IfindClient, thscode: str, name: str | None, limit: int = 8) -> NewsResult:
    """个股资讯（仅背景）：未配置 → disabled；失败 → failed + error；空 → empty。"""
    if not client.enabled:
        return NewsResult(thscode=thscode, status="disabled", error="未配置 IFIND_MCP_TOKEN，资讯功能未启用")
    tool_name: str | None = None
    args: dict[str, Any] = {}
    try:
        tool = pick_news_tool(await client.list_tools())
        if tool is None:
            return NewsResult(thscode=thscode, status="failed", error="iFinD MCP 工具列表中未找到资讯类工具（news/新闻/资讯/公告）")
        tool_name = str(tool["name"])
        args = build_news_arguments(tool, thscode, name, limit)
        result = await client.call(tool_name, args)
        items = parse_news_items(result, limit)
    except IfindError as e:
        return NewsResult(thscode=thscode, status="failed", error=str(e), source=_news_source(tool_name, args))
    except Exception as e:  # 解析等意外错误：资讯只是背景，不影响主流程
        log.warning("iFinD 资讯获取异常", exc_info=True)
        return NewsResult(thscode=thscode, status="failed", error=f"资讯获取异常：{type(e).__name__}", source=_news_source(tool_name, args))
    source = _news_source(tool_name, args)
    if not items:
        return NewsResult(thscode=thscode, status="empty", source=source)
    return NewsResult(thscode=thscode, status="ok", items=items, source=source)


def _news_source(tool_name: str | None, args: Mapping[str, Any]) -> SourceRef | None:
    if tool_name is None:
        return None
    return SourceRef(provider="ifind", endpoint=f"tools/call · {tool_name}", params=dict(args), fetched_at=now_cn())
