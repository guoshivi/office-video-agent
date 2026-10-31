"""飞书开放平台 HTTP 客户端。

只负责三件事：拿 tenant_access_token、收发消息、下载消息里的资源文件。
长连接事件接收在 bot.py，用官方 SDK；这里不依赖 SDK，方便用 mock 传输层做测试。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://open.feishu.cn"
TOKEN_SAFETY_WINDOW = 120  # 提前两分钟换 token，避免边界过期


class FeishuError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    base_url: str = DEFAULT_BASE
    base_app_token: str = ""
    property_table_id: str = ""
    task_table_id: str = ""
    public_base_url: str = ""

    @classmethod
    def from_env(cls) -> "FeishuConfig":
        return cls(
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            base_url=(os.getenv("FEISHU_BASE_URL", DEFAULT_BASE).strip() or DEFAULT_BASE).rstrip("/"),
            base_app_token=os.getenv("FEISHU_BASE_APP_TOKEN", "").strip(),
            property_table_id=os.getenv("FEISHU_PROPERTY_TABLE_ID", "").strip(),
            task_table_id=os.getenv("FEISHU_TASK_TABLE_ID", "").strip(),
            public_base_url=os.getenv("OVA_PUBLIC_URL", "").strip(),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def has_bitable(self) -> bool:
        return bool(self.has_credentials and self.base_app_token and self.task_table_id)


class FeishuClient:
    def __init__(self, config: FeishuConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._transport = transport
        self._token = ""
        self._token_expire_at = 0.0

    # ---------------------------------------------------------------- 基础请求
    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.base_url,
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
            # 本机常有系统代理，访问飞书不能走代理，否则 token 请求会被劫持。
            trust_env=False,
        )

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expire_at:
            return self._token
        if not self.config.has_credentials:
            raise FeishuError("尚未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
        with self._client(15) as client:
            response = client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            )
        payload = _json_or_raise(response, "获取 tenant_access_token 失败")
        if payload.get("code") != 0:
            raise FeishuError(
                f"获取 tenant_access_token 失败：{payload.get('msg')}", payload.get("code")
            )
        self._token = payload["tenant_access_token"]
        self._token_expire_at = time.time() + max(
            60, int(payload.get("expire", 7200)) - TOKEN_SAFETY_WINDOW
        )
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        timeout: float = 30,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        with self._client(timeout) as client:
            response = client.request(
                method, path, headers=headers, json=json_body, params=params
            )
        return _json_or_raise(response, f"调用 {path} 失败")

    def get_json(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    # ------------------------------------------------------------------ 消息
    def send_text(self, chat_id: str, text: str) -> dict:
        return self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    def reply_text(self, message_id: str, text: str) -> dict:
        return self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    def upload_file(self, path: Path, *, file_type: str = "mp4", duration_ms: int = 0) -> str:
        token = self._access_token()
        with self._client(180) as client:
            with Path(path).open("rb") as handle:
                response = client.post(
                    "/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": file_type, "file_name": Path(path).name,
                          "duration": str(duration_ms)},
                    files={"file": (Path(path).name, handle, "video/mp4")},
                )
        payload = _json_or_raise(response, "上传文件到飞书失败")
        if payload.get("code") != 0 or "data" not in payload:
            raise FeishuError(f"上传文件到飞书失败：{payload.get('msg')}", payload.get("code"))
        return payload["data"]["file_key"]

    def send_video(self, chat_id: str, file_key: str) -> dict:
        return self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json_body={
                "receive_id": chat_id,
                "msg_type": "media",
                "content": json.dumps({"file_key": file_key}),
            },
        )

    # ------------------------------------------------------------------ 资源
    def download_resource(
        self, message_id: str, file_key: str, destination: Path, *, kind: str = "file"
    ) -> int:
        """把消息里的视频/文件落到本地磁盘，返回字节数。

        飞书对视频消息的 file_key 也要用 type=file 取；图片才用 type=image。
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        total = 0
        with self._client(600) as client:
            with client.stream(
                "GET",
                f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                headers=headers,
                params={"type": kind},
            ) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", "replace")[:500]
                    raise FeishuError(
                        f"下载消息资源失败（HTTP {response.status_code}）：{body}"
                    )
                with destination.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        output.write(chunk)
        if total == 0:
            destination.unlink(missing_ok=True)
            raise FeishuError("下载到的文件为空，可能是权限不足或消息已撤回")
        return total

    # ---------------------------------------------------------------- 多维表格
    def bitable_create_record(self, table_id: str, fields: dict) -> dict:
        return self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.config.base_app_token}/tables/{table_id}/records",
            json_body={"fields": fields},
        )

    def bitable_update_record(self, table_id: str, record_id: str, fields: dict) -> dict:
        return self._request(
            "PUT",
            f"/open-apis/bitable/v1/apps/{self.config.base_app_token}"
            f"/tables/{table_id}/records/{record_id}",
            json_body={"fields": fields},
        )

    def bitable_search(self, table_id: str, field_name: str, value: str) -> list[dict]:
        payload = self._request(
            "POST",
            f"/open-apis/bitable/v1/apps/{self.config.base_app_token}"
            f"/tables/{table_id}/records/search",
            params={"page_size": 20},
            json_body={
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {
                            "field_name": field_name,
                            "operator": "is",
                            "value": [value],
                        }
                    ],
                }
            },
        )
        return payload.get("data", {}).get("items", []) or []


def _json_or_raise(response: httpx.Response, hint: str) -> dict:
    if response.status_code >= 400:
        raise FeishuError(f"{hint}（HTTP {response.status_code}）：{response.text[:300]}")
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as error:
        raise FeishuError(f"{hint}：返回内容不是 JSON") from error
