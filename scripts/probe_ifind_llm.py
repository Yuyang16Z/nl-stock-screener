"""探测 iFinD MCP 可用工具与 DeepSeek 接口（只打印结构，不落盘数据）。

用法：cd backend && uv run python ../scripts/probe_ifind_llm.py [--call 工具名 '{"参数":"值"}']
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import get_settings  # noqa: E402

S = get_settings()


def parse_body(r: httpx.Response) -> dict:
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        last = None
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    last = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    pass
        return last or {"_raw": r.text[:300]}
    try:
        return r.json()
    except Exception:
        return {"_http": r.status_code, "_raw": r.text[:300]}


async def ifind(call: tuple[str, dict] | None):
    print("## iFinD MCP")
    if not S.ifind_mcp_token:
        print("未配置 IFIND_MCP_TOKEN")
        return
    h = {"Authorization": S.ifind_mcp_token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(S.ifind_mcp_url, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "screenlens-probe", "version": "0.1"}}})
        sid = r.headers.get("mcp-session-id")
        print(f"initialize http={r.status_code} session={'有' if sid else '无'} ct={r.headers.get('content-type')}")
        print("  ", str(parse_body(r))[:300])
        if sid:
            h["Mcp-Session-Id"] = sid
        await c.post(S.ifind_mcp_url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = await c.post(S.ifind_mcp_url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        body = parse_body(r)
        tools = (body.get("result") or {}).get("tools") or []
        print(f"tools/list http={r.status_code} 工具数={len(tools)}")
        for t in tools:
            props = list(((t.get("inputSchema") or {}).get("properties") or {}).keys())
            print(f"  - {t['name']}: {(t.get('description') or '')[:70]} | 参数={props}")
        if call:
            name, args = call
            r = await c.post(S.ifind_mcp_url, headers=h, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}})
            print(f"\ncall {name} http={r.status_code}:")
            print(json.dumps(parse_body(r), ensure_ascii=False)[:3000])


async def llm():
    print("\n## LLM")
    if not S.llm_api_key:
        print("未配置 LLM_API_KEY")
        return
    h = {"Authorization": f"Bearer {S.llm_api_key}"}
    async with httpx.AsyncClient(base_url=S.llm_base_url, headers=h, timeout=60) as c:
        r = await c.get("/models")
        print(f"/models http={r.status_code}", [m.get("id") for m in (r.json().get("data") or [])] if r.status_code == 200 else r.text[:200])
        r = await c.post("/chat/completions", json={
            "model": S.llm_model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": "只输出 JSON。"},
                         {"role": "user", "content": "把“估值合理”解释成一个筛选条件，输出 {\"metric\":..., \"op\":..., \"value\":...}"}]})
        print(f"chat http={r.status_code}", r.json()["choices"][0]["message"]["content"][:300] if r.status_code == 200 else r.text[:300])


async def main():
    call = None
    if len(sys.argv) >= 4 and sys.argv[1] == "--call":
        call = (sys.argv[2], json.loads(sys.argv[3]))
    await ifind(call)
    await llm()


if __name__ == "__main__":
    asyncio.run(main())
