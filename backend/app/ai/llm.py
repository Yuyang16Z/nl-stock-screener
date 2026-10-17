"""OpenAI 兼容的大模型客户端（DESIGN §6.1）。

POST {llm_base_url}/chat/completions，Authorization: Bearer <key>；JSON 模式用 response_format=json_object。
单次请求超时 settings.llm_timeout_s；JSON 解析失败时带错误提示重试 1 次；所有失败统一抛 LLMError(kind, message)。
错误信息中不包含任何密钥。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import httpx

from ..config import Settings

LLMErrorKind = Literal["disabled", "timeout", "http", "invalid_json", "other"]
Message = dict[str, str]

_RETRY_HINT = (
    "你上一条回复不是合法的 JSON（{error}）。请严格按要求的结构重新输出：只输出一个 JSON 对象，"
    "不要包含 Markdown 代码块、注释或任何解释文字。"
)
_FENCE = re.compile(r"^```(?:json|JSON)?\s*(.*?)\s*```$", re.S)


class LLMError(Exception):
    """大模型调用失败。kind: disabled | timeout | http | invalid_json | other。"""

    def __init__(self, kind: LLMErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: LLMErrorKind = kind
        self.message = message

    def __str__(self) -> str:
        return self.message


def extract_json_object(content: str) -> dict[str, Any]:
    """解析模型回复中的 JSON 对象：容忍 ```json 代码块与前后多余文字。失败抛 ValueError（中文原因）。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("回复为空")
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as first:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"无法解析为 JSON（第 {first.lineno} 行第 {first.colno} 列）") from first
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as second:
            raise ValueError(f"无法解析为 JSON（第 {second.lineno} 行第 {second.colno} 列）") from second
    if not isinstance(data, dict):
        raise ValueError("顶层不是 JSON 对象")
    return data


def _message_content(payload: Any) -> str | None:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


class LLMClient:
    """DeepSeek 等 OpenAI 兼容接口的最小客户端。

    client 可注入共享的 httpx.AsyncClient（由调用方管理生命周期）；不注入时每次请求临时创建。
    """

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.settings.llm_api_key)

    @property
    def model(self) -> str:
        return self.settings.llm_model

    @property
    def endpoint(self) -> str:
        return self.settings.llm_base_url.rstrip("/") + "/chat/completions"

    async def chat_json(self, system: str, user: str, *, temperature: float = 0.0, max_tokens: int = 2000) -> dict[str, Any]:
        """返回模型输出的 JSON 对象；内容不是合法 JSON 时附错误提示重试 1 次，仍失败抛 LLMError(invalid_json)。"""
        messages: list[Message] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        content = await self._complete(messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        try:
            return extract_json_object(content)
        except ValueError as first:
            retry = [
                *messages,
                {"role": "assistant", "content": content[:4000]},
                {"role": "user", "content": _RETRY_HINT.format(error=first)},
            ]
            content = await self._complete(retry, temperature=temperature, max_tokens=max_tokens, json_mode=True)
            try:
                return extract_json_object(content)
            except ValueError as second:
                raise LLMError("invalid_json", f"AI 输出不是合法 JSON（已重试 1 次）：{second}") from second

    async def chat_text(self, system: str, user: str, *, temperature: float = 0.3, max_tokens: int = 1200) -> str:
        """返回纯文本回复（去首尾空白）；空回复抛 LLMError(other)。"""
        messages: list[Message] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        text = (await self._complete(messages, temperature=temperature, max_tokens=max_tokens, json_mode=False)).strip()
        if not text:
            raise LLMError("other", "AI 返回了空内容")
        return text

    # ── 内部 ──

    def _scrub(self, text: str) -> str:
        key = self.settings.llm_api_key
        return text.replace(key, "***") if key else text

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(self.settings.llm_timeout_s)
        if self._client is not None:
            return await self._client.post(self.endpoint, json=body, headers=headers, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(self.endpoint, json=body, headers=headers)

    async def _complete(self, messages: list[Message], *, temperature: float, max_tokens: int, json_mode: bool) -> str:
        if not self.enabled:
            raise LLMError("disabled", "未配置大模型 API Key（LLM_API_KEY）")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            resp = await self._post(body)
        except httpx.TimeoutException as e:
            raise LLMError("timeout", f"AI 接口超时（超过 {self.settings.llm_timeout_s:g} 秒）") from e
        except httpx.HTTPError as e:
            raise LLMError("other", f"AI 接口网络错误（{type(e).__name__}）") from e
        if resp.status_code >= 400:
            raise LLMError("http", self._scrub(f"AI 接口返回 HTTP {resp.status_code}{self._error_detail(resp)}"))
        try:
            payload = resp.json()
        except ValueError as e:
            raise LLMError("other", "AI 接口响应不是 JSON") from e
        content = _message_content(payload)
        if content is None:
            raise LLMError("other", "AI 接口响应缺少 choices[0].message.content")
        return content

    @staticmethod
    def _error_detail(resp: httpx.Response) -> str:
        try:
            err = resp.json().get("error")
        except (ValueError, AttributeError):
            return ""
        msg = err.get("message") if isinstance(err, dict) else err if isinstance(err, str) else None
        return f"：{str(msg)[:120]}" if msg else ""
