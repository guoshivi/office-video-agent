"""飞书长连接机器人。

为什么用长连接而不是 Webhook：朋友的电脑在公司内网、没有公网 IP，
Webhook 需要暴露端口或做内网穿透，长连接只要有出网就能收事件。

关键约束（官方 SDK 源码已确认）：事件处理器返回之后 SDK 才回 ACK，
而飞书只给 3 秒。所以处理器里不做任何业务，只把事件丢进队列立刻 return。
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone

from ..db import utc_now
from .client import FeishuClient, FeishuConfig
from .handlers import FeishuAgent, claim_event

logger = logging.getLogger(__name__)

try:  # SDK 缺失时应用仍要能启动，只是飞书入口不可用
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    LARK_AVAILABLE = True
except Exception:  # pragma: no cover - 只在未安装依赖时触发
    lark = None  # type: ignore
    P2ImMessageReceiveV1 = object  # type: ignore
    LARK_AVAILABLE = False


class FeishuBot:
    def __init__(self, config: FeishuConfig | None = None):
        self.config = config or FeishuConfig.from_env()
        self.client = FeishuClient(self.config)
        self.agent = FeishuAgent(self.client)
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.state = "stopped"
        self.last_error = ""
        self.last_event_at = ""
        self.last_action = ""
        self.started_at = ""

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        with self._lock:
            if self._ws_thread and self._ws_thread.is_alive():
                return True
            if not self.config.has_credentials:
                self.state = "unconfigured"
                self.last_error = "缺少 FEISHU_APP_ID / FEISHU_APP_SECRET"
                return False
            if not LARK_AVAILABLE:
                self.state = "error"
                self.last_error = "未安装 lark-oapi，请先执行 pip install -r requirements.txt"
                return False

            self._stop.clear()
            self._spawn_workers()
            self.state = "connecting"
            self.started_at = utc_now()
            self._ws_thread = threading.Thread(
                target=self._run_ws, name="feishu-ws", daemon=True
            )
            self._ws_thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()
        self.state = "stopped"

    def _spawn_workers(self, count: int = 2) -> None:
        self._workers = [
            worker for worker in self._workers if worker.is_alive()
        ]
        for index in range(count - len(self._workers)):
            worker = threading.Thread(
                target=self._worker_loop, name=f"feishu-worker-{index}", daemon=True
            )
            worker.start()
            self._workers.append(worker)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                action = self.agent.handle_event(payload)
                self.last_action = action
                self.last_event_at = utc_now()
            except Exception:
                logger.exception("飞书事件处理失败")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ 长连接
    def _run_ws(self) -> None:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        try:
            self.state = "running"
            client.start()  # 阻塞，内部自带重连
        except Exception as error:  # 连接失败不影响 WebUI
            logger.exception("飞书长连接异常退出")
            self.state = "error"
            self.last_error = str(error)[:500]

    def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """必须在毫秒级返回：SDK 在处理器返回后才回 ACK。"""
        try:
            payload = _normalize_event(data)
        except Exception:
            logger.exception("解析飞书事件失败")
            return
        if payload is None:
            return
        if not claim_event(payload["message_id"], payload["chat_id"], payload["message_type"]):
            logger.info("忽略重复事件 %s", payload["message_id"])
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            logger.warning("飞书事件队列已满，丢弃消息 %s", payload["message_id"])

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict:
        queued = self._queue.qsize()
        return {
            "configured": self.config.has_credentials,
            "enabled": self.state in {"running", "connecting"},
            "state": self.state,
            "sdk_available": LARK_AVAILABLE,
            "connected": self.state == "running",
            "bitable_configured": self.config.has_bitable,
            "queued": queued,
            "workers": sum(1 for worker in self._workers if worker.is_alive()),
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "last_action": self.last_action,
            "last_error": self.last_error,
            "hint": _state_hint(self.state, self.config, LARK_AVAILABLE),
        }


def _state_hint(state: str, config: FeishuConfig, sdk: bool) -> str:
    if not sdk:
        return "安装依赖后重启：pip install -r requirements.txt"
    if not config.has_credentials:
        return "在 .env 填写 FEISHU_APP_ID 与 FEISHU_APP_SECRET 后重启"
    if state == "running":
        return "长连接已建立，可以直接在飞书里给机器人发视频"
    if state == "connecting":
        return "正在建立长连接…"
    if state == "error":
        return "连接失败，检查应用是否已发布、是否开启长连接事件订阅"
    return "未启动"


def _normalize_event(data) -> dict | None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    if message is None:
        return None
    sender = getattr(event, "sender", None)
    sender_type = getattr(sender, "sender_type", "") or ""
    if sender_type == "app":  # 忽略机器人自己发的消息，避免自我循环
        return None
    sender_id = getattr(sender, "sender_id", None)
    return {
        "message_id": getattr(message, "message_id", "") or "",
        "chat_id": getattr(message, "chat_id", "") or "",
        "chat_type": getattr(message, "chat_type", "") or "",
        "message_type": (getattr(message, "message_type", "") or "").lower(),
        "content": getattr(message, "content", "") or "",
        "sender_open_id": getattr(sender_id, "open_id", "") if sender_id else "",
        "create_time": getattr(message, "create_time", "") or "",
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


def _already_handled(message_id: str) -> bool:
    if not message_id:
        return False
    with transaction() as connection:
        row = connection.execute(
            "SELECT 1 FROM feishu_events WHERE message_id=? LIMIT 1", (message_id,)
        ).fetchone()
    return row is not None


_bot: FeishuBot | None = None
_bot_lock = threading.Lock()


def get_bot() -> FeishuBot:
    global _bot
    with _bot_lock:
        if _bot is None:
            _bot = FeishuBot()
        return _bot


def start_bot() -> FeishuBot:
    bot = get_bot()
    bot.start()
    return bot
