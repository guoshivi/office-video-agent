"""一个够用就好的 MCP Streamable HTTP 客户端。

我们只有一个消费方（小红书发布服务），所以不引入官方 SDK，
只实现三件事：握手拿 session、调工具、剥结果文本。

**必须 trust_env=False**：这台机器的 HTTP_PROXY 指向本地代理，
连 127.0.0.1 的请求也会被代理接管并返回 502（实测）。
朋友电脑上如果装了 Clash/加速器，同样症状——这不是服务挂了。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"


@dataclass
class ToolResult:
    text: str = ""
    is_error: bool = False
    # base64 数据，不带 data: 前缀；二维码走这里
    images: list[str] = field(default_factory=list)


class McpError(RuntimeError):
    pass


class McpHttpClient:
    def __init__(self, url: str, token: str = "", timeout: float = 600.0) -> None:
        self.url = url
        self._client = httpx.Client(trust_env=False, timeout=timeout)
        self._headers = {
            "Content-Type": "application/json",
            # Streamable HTTP 要求同时接受 JSON 和 SSE，服务端可能二选一回。
            "Accept": "application/json, text/event-stream",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._session_id: str | None = None
        self._next_id = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "McpHttpClient":
        self.initialize()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------- 协议层 ----------

    def _post(self, payload: dict, capture_session: bool = False) -> httpx.Response:
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = self._client.post(self.url, headers=headers, json=payload)
        except httpx.HTTPError as error:
            raise McpError(
                f"连不上发布服务 {self.url}（{error.__class__.__name__}）。"
                "检查服务是否启动、端口是否被占用；如果本机开了代理，确认已排除 127.0.0.1。"
            ) from error
        if capture_session:
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
        if response.status_code >= 400:
            raise McpError(f"发布服务返回 {response.status_code}：{response.text[:300]}")
        return response

    def _parse(self, response: httpx.Response) -> dict:
        content_type = response.headers.get("Content-Type", "")
        body = response.text
        if "text/event-stream" in content_type:
            # 逐个事件找带 result/error 的那条；心跳注释行（以 : 开头）直接跳过。
            for line in body.splitlines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    continue
            raise McpError(f"SSE 响应里没有可解析的事件：{body[:200]}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise McpError(f"响应不是 JSON：{body[:200]}") from error

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        data = self._parse(self._post(payload, capture_session=(method == "initialize")))
        if "error" in data:
            error = data["error"]
            raise McpError(f"{method} 失败：{error.get('message') or error}")
        return data.get("result") or {}

    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "office-video-agent", "version": "1.0"},
            },
        )
        # 规范要求握手后发一个通知；服务端不认也无所谓，失败不影响后续调用。
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except McpError:
            logger.debug("notifications/initialized 未被接受，忽略")
        return result

    # ---------- 工具层 ----------

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> "ToolResult":
        """调用工具并把返回内容拆成文本、图片和错误标记。

        MCP 的 content 是数组，可能同时含 text 和 image 块。
        二维码这类返回值就只在 image 块里——只读 text 会拿到一句
        「请扫码登录 👇」而没有任何图片（实测踩过）。
        """
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        texts: list[str] = []
        images: list[str] = []
        for item in result.get("content", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                texts.append(item["text"])
            elif item.get("type") == "image" and item.get("data"):
                images.append(item["data"])
        return ToolResult(
            text="\n".join(texts).strip(),
            is_error=bool(result.get("isError")),
            images=images,
        )


def looks_like_failure(text: str) -> bool:
    """把「✅/❌」前缀和各家中英文失败措辞统一收敛成一个判断。"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith("❌") or lowered.startswith("×"):
        return True
    return any(
        token in lowered
        for token in ("失败", "错误", "未登录", "error", "failed", "exception", "超时")
    )
