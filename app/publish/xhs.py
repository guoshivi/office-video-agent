"""小红书发布（走 xpzouying/xiaohongshu-mcp 的 MCP 服务）。

为什么是它而不是自己写浏览器自动化：
登录态、请求签名、风控对抗都在那个 exe 里做完了，我们只负责调用。
代价是它是个黑盒，所以这里对它的返回文本做宽容解析，解析不出来就标记为
"已提交待确认"，而不是假装成功——发布这种动作宁可让人再确认一次。

已验证（v2.5.5，Windows x64）：
- initialize 握手成功，serverInfo: xiaohongshu-mcp 2.0.0
- tools/list 返回 19 个工具，含 publish_with_video
- check_login_status 正常返回「❌ 未登录」
未验证：真实登录后的发布结果文本格式。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

from .mcp_client import McpError, McpHttpClient, looks_like_failure

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://127.0.0.1:18060/mcp"


@dataclass
class PublishResult:
    ok: bool
    message: str
    remote_id: str = ""
    remote_url: str = ""
    raw: str = ""
    # 提交上去但读不出结果——不能当成功，也不能当失败，需要人去看一眼
    uncertain: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "uncertain": self.uncertain,
            "message": self.message,
            "remote_id": self.remote_id,
            "remote_url": self.remote_url,
            "raw": self.raw[:1000],
        }


class XhsPublisher:
    def __init__(self, mcp_url: str = "", token: str = "") -> None:
        self.mcp_url = (mcp_url or DEFAULT_MCP_URL).strip()

        self.token = (token or "").strip()

    def _client(self) -> McpHttpClient:
        return McpHttpClient(self.mcp_url, self.token, timeout=600.0)

    def status(self, timeout: float = 8.0) -> dict:
        """探活 + 登录态。

        超时给 8 秒而不是发布用的 600 秒：这个函数会被首页体检调用，
        服务真的卡住时，宁可显示"连不上"也不要把首屏转成白屏。
        """
        try:
            with McpHttpClient(self.mcp_url, self.token, timeout=timeout) as client:
                result = client.call_tool("check_login_status")
        except McpError as error:
            return {"reachable": False, "logged_in": False, "message": str(error)}
        logged_in = not looks_like_failure(result.text) and not result.is_error
        return {
            "reachable": True,
            "logged_in": logged_in,
            "message": result.text or ("已登录" if logged_in else "未登录"),
        }

    def login_qrcode(self, out_dir: Path) -> dict:
        """取登录二维码并存成 PNG。

        返回 base64 让 WebUI 直接内联显示，朋友扫一下就行，
        不用去命令行里敲东西。
        """
        try:
            with self._client() as client:
                result = client.call_tool("get_login_qrcode")
        except McpError as error:
            return {"ok": False, "message": str(error)}

        # 服务把二维码放在 image 内容块里；旧版本可能是文本里的 base64。
        data_url = result.images[0] if result.images else _extract_base64_image(result.text)
        if not data_url:
            return {"ok": False, "message": result.text[:300] or "没拿到二维码"}

        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "xhs_login_qrcode.png"
        try:
            target.write_bytes(base64.b64decode(data_url))
        except (ValueError, OSError) as error:
            logger.warning("二维码落盘失败：%s", error)
        return {
            "ok": True,
            "image_base64": data_url,
            "path": str(target),
            "raw": result.text[:300],
        }

    def publish_video(
        self,
        title: str,
        content: str,
        video: Path,
        tags: list[str] | None = None,
        visibility: str = "",
        schedule_at: str = "",
    ) -> PublishResult:
        arguments: dict = {
            "title": title,
            "content": content,
            "video": str(video),
        }
        if tags:
            arguments["tags"] = list(tags)
        if visibility:
            arguments["visibility"] = visibility
        if schedule_at:
            arguments["schedule_at"] = schedule_at

        try:
            with self._client() as client:
                result = client.call_tool("publish_with_video", arguments)
        except McpError as error:
            return PublishResult(False, str(error))

        text = result.text
        if result.is_error or looks_like_failure(text):
            return PublishResult(False, text or "发布失败（服务未给出原因）", raw=text)

        remote_id = _extract_note_id(text)
        return PublishResult(
            ok=True,
            # 服务一般只回一句「发布成功」，拿不到笔记 ID 时如实说明，
            # 免得下游以为能直接拼出链接。
            message=text or "已提交发布",
            remote_id=remote_id,
            remote_url=f"https://www.xiaohongshu.com/explore/{remote_id}" if remote_id else "",
            raw=text,
            uncertain=not remote_id,
        )


def _extract_base64_image(text: str) -> str:
    """二维码可能在纯 base64 里，也可能带 data:image/png;base64, 前缀。"""
    if not text:
        return ""
    for token in text.split():
        candidate = token.strip().strip('"')
        if candidate.startswith("data:image"):
            return candidate.split(",", 1)[-1]
        # 一段像 base64 的长串（PNG 的 base64 通常以 iVBOR 开头）
        if len(candidate) > 200 and candidate.startswith("iVBOR"):
            return candidate
    marker = "base64,"
    if marker in text:
        return text.split(marker, 1)[-1].strip().strip('"').split()[0]
    return ""


def _extract_note_id(text: str) -> str:
    """尽量从返回文本里捞 24 位十六进制笔记 ID；捞不到就返回空。"""
    import re

    for match in re.finditer(r"\b([0-9a-f]{24})\b", text or ""):
        return match.group(1)
    return ""
