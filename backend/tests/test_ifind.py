"""ifind：MCP streamable HTTP 的 JSON 与 SSE 两种响应、未配置、JSON-RPC 错误与结果解析（全部为虚构资讯）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from app.data.ifind import (
    PROTOCOL_VERSION,
    IfindClient,
    IfindError,
    build_news_arguments,
    get_stock_news,
    parse_news_items,
    parse_sse_events,
    pick_news_tool,
)

try:
    from _fixtures_data import IFIND_URL, make_settings
except ImportError:  # pragma: no cover
    from tests._fixtures_data import IFIND_URL, make_settings

NEWS_TOOL = {
    "name": "search_stock_news",
    "description": "查询个股新闻资讯",
    "inputSchema": {
        "type": "object",
        "properties": {
            "stock_code": {"type": "string", "description": "股票代码，如 600001.SH"},
            "page_size": {"type": "integer", "maximum": 20},
        },
        "required": ["stock_code"],
    },
}
QUOTE_TOOL = {"name": "get_quote", "description": "获取行情快照", "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}}}
NEWS_JSON = {
    "data": {
        "list": [
            {"title": "测试甲发布半年度报告（虚构）", "publish_time": 1786000000000, "source": "测试来源", "url": "https://news.example.test/a1", "summary": "虚构摘要一"},
            {"title": "测试甲召开业绩说明会（虚构）", "publish_time": "2026-09-01 10:00", "source": "测试来源二", "url": "https://news.example.test/a2"},
            {"title": "测试甲发布半年度报告（虚构）", "publish_time": 1786000000000},
        ]
    }
}


class FakeMcp:
    """假 MCP 服务端：initialize 返回会话头；notifications 返回 202；其余按方法返回结果。"""

    def __init__(
        self,
        *,
        sse: bool = False,
        tools: list[dict[str, Any]] | None = None,
        call_result: dict[str, Any] | None = None,
        call_error: dict[str, Any] | None = None,
        session: str = "sess-123",
    ) -> None:
        self.sse = sse
        self.tools = tools if tools is not None else [QUOTE_TOOL, NEWS_TOOL]
        self.call_result = call_result if call_result is not None else {"content": [{"type": "text", "text": json.dumps(NEWS_JSON, ensure_ascii=False)}], "isError": False}
        self.call_error = call_error
        self.session = session
        self.requests: list[tuple[dict[str, Any], httpx.Headers]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((body, request.headers))
        method = body.get("method")
        headers: dict[str, str] = {}
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-ifind", "version": "0"}}}
            headers["Mcp-Session-Id"] = self.session
        elif method == "tools/list":
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self.tools}}
        elif method == "tools/call":
            if self.call_error:
                msg = {"jsonrpc": "2.0", "id": body["id"], "error": self.call_error}
            else:
                msg = {"jsonrpc": "2.0", "id": body["id"], "result": self.call_result}
        else:
            msg = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "Method not found"}}
        if not self.sse:
            return httpx.Response(200, headers=headers, json=msg)
        text = json.dumps(msg, ensure_ascii=False)
        cut = text.find(",") + 1  # 拆成多行 data:
        note = json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "x"}})
        stream = f": keep-alive\n\nevent: message\ndata: {note}\n\nevent: message\nid: 1\ndata: {text[:cut]}\ndata: {text[cut:]}\n\n"
        return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"}, content=stream.encode("utf-8"))

    def methods(self) -> list[str]:
        return [b.get("method", "") for b, _ in self.requests]


def client_with_token(token: str | None = "tok-test") -> IfindClient:
    return IfindClient(make_settings(ifind_mcp_token=token))


@pytest.mark.parametrize("sse", [False, True])
async def test_news_flow_json_and_sse(sse: bool) -> None:
    fake = FakeMcp(sse=sse)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(IFIND_URL).mock(side_effect=fake)
        client = client_with_token()
        res = await get_stock_news(client, "600001.SH", "测试甲", limit=5)
        await client.aclose()
    assert res.status == "ok", res.error
    assert [i.title for i in res.items] == ["测试甲发布半年度报告（虚构）", "测试甲召开业绩说明会（虚构）"]  # 去重
    first = res.items[0]
    assert first.source == "测试来源" and first.url == "https://news.example.test/a1"
    assert first.time is not None and first.time.startswith("2026-")
    assert first.summary == "虚构摘要一"
    assert res.note == "资讯仅作背景参考，不参与筛选与打分。"
    assert res.source is not None and res.source.provider == "ifind" and "search_stock_news" in (res.source.endpoint or "")
    assert fake.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    init_body, init_headers = fake.requests[0]
    assert init_body["params"]["protocolVersion"] == "2025-03-26"
    assert init_body["params"]["capabilities"] == {} and init_body["params"]["clientInfo"]["name"]
    assert "mcp-session-id" not in init_headers
    for body, headers in fake.requests:
        assert headers["authorization"] == "tok-test"  # 不加 Bearer 前缀
        assert "application/json" in headers["accept"] and "text/event-stream" in headers["accept"]
        assert headers["content-type"].startswith("application/json")
    for _, headers in fake.requests[1:]:
        assert headers["mcp-session-id"] == "sess-123"
    call_body = fake.requests[3][0]
    assert call_body["params"] == {"name": "search_stock_news", "arguments": {"stock_code": "600001.SH", "page_size": 5}}
    assert route.call_count == 4


async def test_tools_list_is_cached_per_client() -> None:
    fake = FakeMcp()
    with respx.mock(assert_all_called=False) as router:
        router.post(IFIND_URL).mock(side_effect=fake)
        client = client_with_token()
        await get_stock_news(client, "600001.SH", "测试甲")
        await get_stock_news(client, "000007.SZ", "测试庚")
        await client.aclose()
    assert fake.methods().count("initialize") == 1
    assert fake.methods().count("tools/list") == 1
    assert fake.methods().count("tools/call") == 2


async def test_not_configured_is_disabled_without_requests() -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(IFIND_URL).mock(side_effect=FakeMcp())
        client = client_with_token(None)
        res = await get_stock_news(client, "600001.SH", "测试甲")
    assert client.enabled is False
    assert res.status == "disabled" and res.items == []
    assert "IFIND_MCP_TOKEN" in (res.error or "")
    assert route.call_count == 0


async def test_jsonrpc_error_is_failed() -> None:
    fake = FakeMcp(call_error={"code": -32602, "message": "参数错误"})
    with respx.mock(assert_all_called=False) as router:
        router.post(IFIND_URL).mock(side_effect=fake)
        client = client_with_token()
        res = await get_stock_news(client, "600001.SH", "测试甲")
        with pytest.raises(IfindError) as ei:
            await client.call("search_stock_news", {"stock_code": "600001.SH"})
        await client.aclose()
    assert res.status == "failed"
    assert "-32602" in (res.error or "") and "参数错误" in (res.error or "")
    assert ei.value.kind == "rpc_error" and ei.value.code == -32602


async def test_tool_is_error_flag_is_failed() -> None:
    fake = FakeMcp(call_result={"content": [{"type": "text", "text": "token 无效"}], "isError": True})
    with respx.mock(assert_all_called=False) as router:
        router.post(IFIND_URL).mock(side_effect=fake)
        client = client_with_token()
        res = await get_stock_news(client, "600001.SH", "测试甲")
        await client.aclose()
    assert res.status == "failed" and "token 无效" in (res.error or "")


async def test_empty_result_is_empty() -> None:
    fake = FakeMcp(sse=True, call_result={"content": [{"type": "text", "text": "[]"}]})
    with respx.mock(assert_all_called=False) as router:
        router.post(IFIND_URL).mock(side_effect=fake)
        client = client_with_token()
        res = await get_stock_news(client, "600001.SH", "测试甲")
        await client.aclose()
    assert res.status == "empty" and res.items == [] and res.error is None


async def test_no_news_tool_is_failed() -> None:
    fake = FakeMcp(tools=[QUOTE_TOOL])
    with respx.mock(assert_all_called=False) as router:
        router.post(IFIND_URL).mock(side_effect=fake)
        client = client_with_token()
        res = await get_stock_news(client, "600001.SH", "测试甲")
        await client.aclose()
    assert res.status == "failed" and "资讯类工具" in (res.error or "")
    assert "tools/call" not in fake.methods()


async def test_http_401_is_failed() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(IFIND_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
        client = client_with_token()
        res = await get_stock_news(client, "600001.SH", "测试甲")
        await client.aclose()
    assert res.status == "failed" and "401" in (res.error or "")


async def test_timeout_is_failed() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(IFIND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        client = client_with_token()
        res = await get_stock_news(client, "600001.SH", "测试甲")
        await client.aclose()
    assert res.status == "failed" and "超时" in (res.error or "")


def test_pick_news_tool_by_keywords() -> None:
    tools = [
        {"name": "get_quote", "description": "行情"},
        {"name": "ths_feed", "description": "个股资讯与公告查询"},
        {"name": "notice_board", "description": "交易所公告"},
    ]
    assert pick_news_tool(tools)["name"] == "ths_feed"  # 资讯(3)+公告(2)+个股(1) 最高
    assert pick_news_tool([{"name": "a"}, {"name": "b", "description": "无关"}]) is None
    assert pick_news_tool(tools, candidates=["notice_board"])["name"] == "notice_board"


def test_build_news_arguments_variants() -> None:
    nl = {"inputSchema": {"properties": {"query": {"type": "string"}, "limit": {"type": "string"}}}}
    assert build_news_arguments(nl, "600001.SH", "测试甲", 8) == {"query": "测试甲 公司动态 经营 业绩", "limit": "8"}
    kw = {"inputSchema": {"properties": {"keyword": {"type": "array"}, "secu_code": {"type": "array"}, "count": {"type": "integer", "maximum": 5}}}}
    assert build_news_arguments(kw, "600001.SH", "测试甲", 8) == {"keyword": ["测试甲"], "secu_code": ["600001.SH"], "count": 5}
    bare = {"inputSchema": {"properties": {"code": {"type": "string", "description": "6位股票代码，不带后缀"}, "type": {"type": "string", "enum": ["news", "notice"]}}, "required": ["code", "type"]}}
    assert build_news_arguments(bare, "600001.SH", None, 8) == {"code": "600001", "type": "news"}
    assert build_news_arguments({"name": "x"}, "600001.SH", "测试甲", 8) == {}


def test_parse_sse_events() -> None:
    text = (
        ": comment\n\n"
        "event: message\ndata: {\"jsonrpc\":\"2.0\",\n"
        "data: \"id\":7,\"result\":{\"ok\":true}}\n\n"
        "data: not-json\n\n"
        "data:{\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n"
    )
    events = parse_sse_events(text)
    assert events == [{"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}, {"jsonrpc": "2.0", "method": "notifications/progress"}]


def test_parse_news_items_formats() -> None:
    md = (
        "| 标题 | 发布时间 | 来源 | 链接 |\n|---|---|---|---|\n"
        "| 测试公告一（虚构） | 2026-09-01 | 测试来源 | [查看](https://news.example.test/m1) |\n"
        "| 测试公告二（虚构） | 2026-09-02 | 测试来源 | https://news.example.test/m2 |\n"
    )
    items = parse_news_items({"content": [{"type": "text", "text": md}]})
    assert [i.title for i in items] == ["测试公告一（虚构）", "测试公告二（虚构）"]
    assert items[0].url == "https://news.example.test/m1" and items[1].url == "https://news.example.test/m2"
    assert items[0].time == "2026-09-01"

    columnar = {"tables": [{"table": {"title": ["标题A（虚构）", "标题B（虚构）"], "time": [1786000000, 1786100000], "source": ["S1", "S2"]}}]}
    items2 = parse_news_items({"content": [{"type": "text", "text": json.dumps(columnar, ensure_ascii=False)}]})
    assert [i.title for i in items2] == ["标题A（虚构）", "标题B（虚构）"]
    assert items2[0].time is not None and items2[0].time.startswith("2026-")

    structured = parse_news_items({"structuredContent": {"items": [{"标题": "结构化（虚构）", "来源": "测试"}]}, "content": []})
    assert structured[0].title == "结构化（虚构）" and structured[0].source == "测试"

    listed = parse_news_items({"content": [{"type": "text", "text": "1. 测试新闻甲（虚构） https://news.example.test/l1\n2. 测试新闻乙（虚构）"}]})
    assert [i.title for i in listed] == ["测试新闻甲（虚构）", "测试新闻乙（虚构）"]
    assert listed[0].url == "https://news.example.test/l1"

    assert parse_news_items({"content": [{"type": "text", "text": "未找到相关资讯"}]}) == []
    assert parse_news_items(None) == []
    assert len(parse_news_items({"content": [{"type": "text", "text": json.dumps(NEWS_JSON, ensure_ascii=False)}]}, limit=1)) == 1
