"""飞书消息 → 业务动作。

设计原则：
1. 飞书事件必须 3 秒内响应，所以收到视频只做下载落盘 + 回执，绝不同步出片。
2. 出片、状态回写都丢到后台线程，飞书只负责告知结果。
3. 所有动作最终落到和 WebUI 完全相同的表与函数上，两个入口不会跑出两套状态。
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from ..db import FEISHU_DIR, get_settings, transaction, utc_now
from ..services import (
    TASK_SELECT,
    create_task_record,
    find_task_by_no,
    format_task_message,
    set_task_script,
)
from ..workbench_client import render_task
from . import bitable
from .client import FeishuClient, FeishuError
from .intents import (
    HELP_TEXT,
    Intent,
    describe_property,
    parse_command,
    resolve_property,
)

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
SAFE_NAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fa5._-]+")


def safe_filename(name: str, fallback: str = "source.mp4") -> str:
    cleaned = SAFE_NAME.sub("_", Path(name or "").name).strip("._")
    if not cleaned:
        cleaned = fallback
    if Path(cleaned).suffix.lower() not in VIDEO_EXTENSIONS:
        cleaned = f"{Path(cleaned).stem or 'source'}.mp4"
    return cleaned


def human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit in {"B", "KB"} else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


class FeishuAgent:
    """一个 chat 一条会话状态；无状态，可安全地被多个线程调用。"""

    def __init__(self, client: FeishuClient):
        self.client = client

    # ---------------------------------------------------------------- 事件入口
    def handle_event(self, message: dict) -> str:
        chat_id = message.get("chat_id") or ""
        message_id = message.get("message_id") or ""
        msg_type = (message.get("message_type") or "").lower()
        content = message.get("content") or ""
        if not chat_id:
            return "ignored:no_chat"

        try:
            if msg_type in {"media", "file", "video"}:
                action = self._handle_attachment(chat_id, message_id, content)
            elif msg_type == "text":
                action = self._handle_text(chat_id, message_id, content)
            elif msg_type == "audio":
                self.client.send_text(chat_id, "语音暂时不支持，请直接发视频或打字告诉我房源。")
                action = "audio_rejected"
            else:
                self.client.send_text(
                    chat_id,
                    f"暂不支持 {msg_type} 类型消息。发一段办公室视频，再回复房源信息即可。",
                )
                action = f"unsupported:{msg_type}"
        except FeishuError as error:
            logger.warning("飞书动作失败：%s", error)
            action = f"error:{error}"
        except Exception as error:  # 单条消息失败不能拖垮长连接
            logger.exception("处理飞书消息异常")
            action = f"error:{error}"
        _log_event(message_id, chat_id, msg_type, content[:500], action)
        return action

    # ------------------------------------------------------------------ 附件
    def _handle_attachment(self, chat_id: str, message_id: str, content: str) -> str:
        file_key, filename, duration = _parse_media(content)
        if not file_key:
            self.client.send_text(chat_id, "没能读到这个文件，请用「视频」方式重新发一次。")
            return "no_file_key"

        saved_name = safe_filename(filename)
        target_dir = FEISHU_DIR / "inbox" / message_id
        destination = target_dir / saved_name
        size = self.client.download_resource(message_id, file_key, destination)
        if size > MAX_VIDEO_BYTES:
            destination.unlink(missing_ok=True)
            self.client.send_text(chat_id, f"视频 {human_size(size)} 超过 2GB，请压缩后再发。")
            return "too_large"

        _save_session(
            chat_id,
            message_id=message_id,
            video_path=str(destination),
            filename=saved_name,
            size=size,
        )
        hint = "下一步：回复房源信息，例如「A区 中关村大厦 1201」，或只回房号「1201」。"
        self.client.send_text(
            chat_id,
            f"视频已收到（{saved_name} · {human_size(size)}）✅\n"
            f"这一步只保存原片，不会立刻出片。\n{hint}\n"
            f"发送「帮助」查看全部指令。",
        )
        return f"video_saved:{size}:{duration}"

    # ------------------------------------------------------------------ 文本
    def _handle_text(self, chat_id: str, message_id: str, content: str) -> str:
        text = _parse_text(content)
        intent = parse_command(text)

        if intent and intent.action == "help":
            self.client.send_text(chat_id, HELP_TEXT)
            return "help"
        if intent and intent.action == "cancel":
            _clear_session(chat_id)
            self.client.send_text(chat_id, "已取消当前待处理视频，可以重新发视频。")
            return "cancel"
        if intent and intent.action == "render":
            return self._start_render(chat_id, intent)
        if intent and intent.action == "script":
            return self._update_script(chat_id, intent)
        if intent and intent.action == "status":
            return self._report_status(chat_id, intent)
        if intent and intent.action == "search":
            return self._search(chat_id, intent.text or text)

        session = _load_session(chat_id)
        if session and session["pending_video_path"]:
            return self._bind_property(chat_id, text, session)
        # 没有待处理视频：把这句话当房源查询，比直接报错更有用
        return self._search(chat_id, text)

    # -------------------------------------------------------------- 绑定房源
    def _bind_property(self, chat_id: str, text: str, session) -> str:
        video_path = Path(session["pending_video_path"])
        if not video_path.is_file():
            _clear_session(chat_id)
            self.client.send_text(chat_id, "原片已丢失，请重新发一次视频。")
            return "pending_missing"

        result = resolve_property(text)
        if result.kind == "ambiguous":
            lines = [f"「{text.strip()}」匹配到多套房源，回复序号或完整信息："]
            for index, row in enumerate(result.candidates, start=1):
                lines.append(f"{index}. {describe_property(row)}")
            self.client.send_text(chat_id, "\n".join(lines))
            return "ambiguous"
        if result.kind == "none":
            self.client.send_text(
                chat_id,
                f"{result.hint}。\n请按「区域 楼栋 房号」的格式再发一次，例如「A区 中关村大厦 1201」。\n"
                f"发送「取消」可以放弃这段视频。",
            )
            return "no_match"

        property_row = result.property
        settings = get_settings()
        task_id = create_task_record(
            property_row=property_row,
            video_path=video_path,
            original_filename=session["pending_filename"] or video_path.name,
            target_duration=int(settings.get("default_duration", 45) or 45),
            aspect_ratio=settings.get("default_aspect_ratio", "9:16") or "9:16",
            voice=settings.get("default_voice", "female_warm") or "female_warm",
            source="feishu",
            notify_chat_id=chat_id,
        )
        _clear_session(chat_id)
        task = find_task_by_no(_task_no_of(task_id))
        self.client.send_text(chat_id, format_task_message(task, settings))
        bitable.sync_task(self.client, task)

        if str(settings.get("feishu_auto_render", "0")) == "1":
            threading.Thread(
                target=self._render_worker, args=(chat_id, task["id"]), daemon=True
            ).start()
            return f"task_created_auto_render:{task['task_no']}"
        return f"task_created:{task['task_no']}"

    # ------------------------------------------------------------------ 出片
    def _start_render(self, chat_id: str, intent: Intent) -> str:
        if not intent.task_no:
            self.client.send_text(chat_id, "请带上任务编号，例如「出片 V20251101-ABCDE」。")
            return "render_no_task"
        task = find_task_by_no(intent.task_no)
        if task is None:
            self.client.send_text(chat_id, f"没有找到任务 {intent.task_no}。")
            return "render_unknown_task"
        settings = get_settings()
        if not settings.get("workbench_url"):
            self.client.send_text(chat_id, "视频工作台地址还没配置，请先在公司电脑的 WebUI 里填写。")
            return "render_not_configured"
        if task["status"] == "processing":
            self.client.send_text(chat_id, f"任务 {task['task_no']} 正在出片中，稍等。")
            return "render_busy"
        if not task["script"].strip():
            self.client.send_text(chat_id, "这条任务的口播是空的，先「改口播」再出片。")
            return "render_empty_script"

        with transaction() as connection:
            connection.execute(
                """UPDATE tasks SET status='processing', stage='已排队，等待视频工作台',
                   progress=1, error_message='', notify_chat_id=?, updated_at=? WHERE id=?""",
                (chat_id, utc_now(), task["id"]),
            )
        self.client.send_text(
            chat_id, f"已提交出片：{task['task_no']}\n完成后我把成片发回来，先不用等在这里。"
        )
        threading.Thread(target=self._render_worker, args=(chat_id, task["id"]), daemon=True).start()
        return f"render_started:{task['task_no']}"

    def _render_worker(self, chat_id: str, task_id: str) -> None:
        try:
            render_task(task_id)
        except Exception as error:
            logger.exception("飞书触发的出片失败")
            with transaction() as connection:
                connection.execute(
                    """UPDATE tasks SET status='failed', stage='出片失败',
                       error_message=?, updated_at=? WHERE id=?""",
                    (str(error)[:1000], utc_now(), task_id),
                )
        self.notify_result(chat_id, task_id)

    def notify_result(self, chat_id: str, task_id: str) -> None:
        with transaction() as connection:
            row = connection.execute(
                f"""SELECT {TASK_SELECT} FROM tasks t
                    JOIN properties p ON p.id=t.property_id WHERE t.id=?""",
                (task_id,),
            ).fetchone()
        if row is None:
            return
        bitable.sync_task(self.client, row)
        if row["status"] == "completed" and row["output_video_path"]:
            self._send_output(chat_id, row)
        elif row["status"] == "failed":
            self.client.send_text(
                chat_id,
                f"❌ {row['task_no']} 出片失败\n{row['error_message'] or '未知原因'}\n"
                f"可在 WebUI 查看详情后重新提交。",
            )
        else:
            self.client.send_text(
                chat_id,
                f"任务 {row['task_no']} 当前状态：{row['status']} {row['stage']}",
            )

    def _send_output(self, chat_id: str, row) -> None:
        output_path = Path(row["output_video_path"])
        if not output_path.is_file():
            self.client.send_text(chat_id, f"{row['task_no']} 已出片但文件不见了，请在 WebUI 重新导出。")
            return
        size = output_path.stat().st_size
        caption = (
            f"✅ {row['task_no']} 出片完成\n"
            f"{describe_property(row)}\n"
            f"成片 {human_size(size)}，可直接下载发到平台。"
        )
        # 飞书单文件上限 30MB，超了就只发路径，避免上传到一半失败
        if size <= 30 * 1024 * 1024:
            try:
                file_key = self.client.upload_file(output_path, file_type="mp4")
                self.client.send_video(chat_id, file_key)
                self.client.send_text(chat_id, caption)
                return
            except FeishuError as error:
                logger.warning("飞书上传成片失败：%s", error)
        self.client.send_text(chat_id, f"{caption}\n成片较大，请在公司电脑查看：{output_path}")

    # -------------------------------------------------------------- 其它指令
    def _update_script(self, chat_id: str, intent: Intent) -> str:
        if not intent.task_no:
            self.client.send_text(chat_id, "请带上任务编号，例如「改口播 V20251101-ABCDE 新文案」。")
            return "script_no_task"
        text = intent.payload
        if not text:
            self.client.send_text(chat_id, "文案不能为空。")
            return "script_empty"
        task = find_task_by_no(intent.task_no)
        if task is None:
            self.client.send_text(chat_id, f"没有找到任务 {intent.task_no}。")
            return "script_unknown_task"
        if task["status"] == "processing":
            self.client.send_text(chat_id, "这条正在出片，等完成后再改。")
            return "script_busy"
        set_task_script(task["id"], text)
        self.client.send_text(
            chat_id,
            f"口播已更新：{task['task_no']}\n「{text}」\n"
            f"确认无误后回复「出片 {task['task_no']}」。",
        )
        return f"script_updated:{task['task_no']}"

    def _report_status(self, chat_id: str, intent: Intent) -> str:
        if intent.task_no:
            task = find_task_by_no(intent.task_no)
            if task is None:
                self.client.send_text(chat_id, f"没有找到任务 {intent.task_no}。")
                return "status_unknown_task"
            self.client.send_text(chat_id, format_task_message(task, get_settings(), brief=True))
            return f"status:{task['task_no']}"
        with transaction() as connection:
            rows = connection.execute(
                f"""SELECT {TASK_SELECT} FROM tasks t
                    JOIN properties p ON p.id=t.property_id
                    ORDER BY t.created_at DESC LIMIT 5"""
            ).fetchall()
        if not rows:
            self.client.send_text(chat_id, "还没有任何任务。发一段视频开始吧。")
            return "status_empty"
        lines = ["最近 5 条任务："]
        for row in rows:
            lines.append(f"· {row['task_no']} {row['status']} {row['stage']} {row['progress']}%")
        self.client.send_text(chat_id, "\n".join(lines))
        return "status_list"

    def _search(self, chat_id: str, query: str) -> str:
        query = (query or "").strip()
        if not query:
            self.client.send_text(chat_id, "想找什么样的办公室？例如「A区 100平 带家具 朝南」。")
            return "search_empty"
        rows = _keyword_search(query)
        if not rows:
            self.client.send_text(
                chat_id, f"没找到匹配「{query}」的房源。换个说法，比如只发房号或楼栋名。"
            )
            return "search_none"
        lines = [f"「{query}」匹配到 {len(rows)} 套："]
        for row in rows[:5]:
            lines.append(f"· {describe_property(row)}")
        lines.append("要出片请先发视频，再回复上面某个房源。")
        self.client.send_text(chat_id, "\n".join(lines))
        return f"search:{len(rows)}"


# ------------------------------------------------------------------ 会话状态
def _load_session(chat_id: str):
    with transaction() as connection:
        return connection.execute(
            "SELECT * FROM feishu_sessions WHERE chat_id=?", (chat_id,)
        ).fetchone()


def _save_session(chat_id: str, *, message_id: str, video_path: str, filename: str, size: int) -> None:
    with transaction() as connection:
        connection.execute(
            """INSERT INTO feishu_sessions(
                   chat_id, pending_message_id, pending_video_path, pending_filename,
                   pending_size, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   pending_message_id=excluded.pending_message_id,
                   pending_video_path=excluded.pending_video_path,
                   pending_filename=excluded.pending_filename,
                   pending_size=excluded.pending_size,
                   updated_at=excluded.updated_at""",
            (chat_id, message_id, video_path, filename, size, utc_now()),
        )


def _clear_session(chat_id: str) -> None:
    with transaction() as connection:
        connection.execute("DELETE FROM feishu_sessions WHERE chat_id=?", (chat_id,))


def _log_event(message_id: str, chat_id: str, msg_type: str, text: str, action: str) -> None:
    """排队时已插入占位行，这里只补结果，避免同一事件记两条。"""
    with transaction() as connection:
        cursor = connection.execute(
            """UPDATE feishu_events SET chat_id=?, msg_type=?, user_text=?, action=?
               WHERE message_id=?""",
            (chat_id, msg_type, text, action, message_id),
        )
        if cursor.rowcount == 0:
            connection.execute(
                """INSERT INTO feishu_events(
                       message_id, chat_id, msg_type, user_text, action, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (message_id, chat_id, msg_type, text, action, utc_now()),
            )


def claim_event(message_id: str, chat_id: str, msg_type: str) -> bool:
    """飞书会重复投递事件，靠 message_id 占位做幂等。"""
    if not message_id:
        return True
    with transaction() as connection:
        row = connection.execute(
            "SELECT 1 FROM feishu_events WHERE message_id=? LIMIT 1", (message_id,)
        ).fetchone()
        if row:
            return False
        connection.execute(
            """INSERT INTO feishu_events(
                   message_id, chat_id, msg_type, user_text, action, created_at
               ) VALUES (?, ?, ?, '', 'queued', ?)""",
            (message_id, chat_id, msg_type, utc_now()),
        )
    return True


def _task_no_of(task_id: str) -> str:
    with transaction() as connection:
        row = connection.execute("SELECT task_no FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row["task_no"] if row else ""


def _parse_text(content: str) -> str:
    import json

    try:
        return str(json.loads(content or "{}").get("text", "")).strip()
    except (ValueError, TypeError):
        return (content or "").strip()


def _parse_media(content: str) -> tuple[str, str, int]:
    import json

    try:
        data = json.loads(content or "{}")
    except (ValueError, TypeError):
        return "", "", 0
    file_key = data.get("file_key") or data.get("image_key") or ""
    name = data.get("file_name") or ""
    if not name and file_key:
        name = f"feishu-{file_key[-8:]}.mp4"
    return file_key, name, int(data.get("duration") or 0)


def _keyword_search(query: str, limit: int = 5) -> list:
    tokens = [token for token in re.split(r"[\s,，、]+", query) if token]
    clauses, params = [], []
    for token in tokens:
        clauses.append("(region_norm LIKE ? OR building_norm LIKE ? OR room_norm LIKE ? "
                       "OR area_raw LIKE ? OR furnishing_layout_raw LIKE ? OR orientation_raw LIKE ?)")
        params.extend([f"%{token}%"] * 6)
    where = " AND ".join(clauses) if clauses else "1=1"
    with transaction() as connection:
        return connection.execute(
            f"SELECT * FROM properties WHERE {where} AND room_norm<>'' "
            f"ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END LIMIT ?",
            [*params, limit],
        ).fetchall()
