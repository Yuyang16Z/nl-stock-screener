"""llm：JSON 抽取、禁用、HTTP 错误、非法 JSON 重试（httpx.MockTransport，不访问网络）。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ai.llm import LLMClient, LLMError, extract_json_object
from app.config import Settings


def _settings(key: str | None = "sk-test-secret") -> Settings:
    return Settings(_env_file=None, llm_api_key=key, llm_base_url="https://llm.example.test", llm_model="m-test")


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_extract_json_object() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('前缀 {"a": 2} 后缀') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json_object("[1, 2]")


async def test_disabled_raises() -> None:
    with pytest.raises(LLMError) as ei:
        await LLMClient(_settings(None)).chat_json("s", "u")
    assert ei.value.kind == "disabled"


async def test_retry_on_invalid_json_then_ok() -> None:
    replies = iter([_reply("不是 JSON"), _reply('{"ok": true}')])
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return next(replies)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await LLMClient(_settings(), client=client).chat_json("s", "u")
    assert out == {"ok": True} and len(bodies) == 2
    assert bodies[0]["response_format"] == {"type": "json_object"} and bodies[0]["model"] == "m-test"


async def test_http_error_does_not_leak_key() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key sk-test-secret"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMError) as ei:
            await LLMClient(_settings(), client=client).chat_json("s", "u")
    assert ei.value.kind == "http" and "sk-test-secret" not in str(ei.value)
