"""多入口的“先收视频、后绑房源”缓冲层。

WebUI、WorkBuddy 桌面会话、飞书/企业微信机器人都是同一件事：
中介先甩一段视频过来，过一会儿才说“A区 中关村大厦 1201”。
如果收到视频就要求立刻知道房源，中间那几十秒的素材就会丢。

所以这里把原始视频先落到 inbox，记一条 pending 的 channel_clips；
等房源解析成功，再调用共用的 create_task_record 建任务。
解析歧义时不动数据，只把候选返回去让人选。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .db import DATA_DIR, UPLOAD_DIR, transaction, utc_now
from .feishu.intents import describe_property, resolve_property
from .services import create_task_record, get_task, new_task_no, serialize_task

CLIP_DIR = DATA_DIR / "inbox"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
MAX_CLIP_SIZE = 2 * 1024 * 1024 * 1024


class ChannelError(RuntimeError):
    pass


def _row_to_clip(row) -> dict:
    if row is None:
        return {}
    return {
        "id": row["id"],
        "channel": row["channel"],
        "sender": row["sender"],
        "filename": row["filename"],
        "size": row["size"],
        "status": row["status"],
        "task_id": row["task_id"],
        "property_id": row["property_id"],
        "created_at": row["created_at"],
    }


def save_clip(filename: str, stream, *, channel: str = "desktop", sender: str = "") -> dict:
    """接收一段原始视频，落盘为 pending clip。stream 只要有 read(n) 就行。"""
    clean_name = Path(filename or "source.mp4").name
    extension = Path(clean_name).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        raise ChannelError("视频格式支持 MP4、MOV、M4V、AVI、MKV")

    clip_id = uuid.uuid4().hex[:16]
    clip_dir = CLIP_DIR / clip_id
    clip_dir.mkdir(parents=True, exist_ok=True)
    target = clip_dir / f"source{extension}"
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_CLIP_SIZE:
                    raise ChannelError("单个视频不能超过 2 GB")
                output.write(chunk)
        if size == 0:
            raise ChannelError("上传的视频为空")
        now = utc_now()
        with transaction() as connection:
            connection.execute(
                """INSERT INTO channel_clips(
                    id, channel, sender, filename, stored_path, size,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (clip_id, channel, sender, clean_name, str(target), size, now, now),
            )
            row = connection.execute(
                "SELECT * FROM channel_clips WHERE id=?", (clip_id,)
            ).fetchone()
    except Exception:
        shutil.rmtree(clip_dir, ignore_errors=True)
        raise
    return _row_to_clip(row)


def get_clip(clip_id: str) -> dict:
    with transaction() as connection:
        row = connection.execute(
            "SELECT * FROM channel_clips WHERE id=?", (clip_id,)
        ).fetchone()
    return _row_to_clip(row)


def latest_pending(channel: str = "", sender: str = "") -> dict:
    """取最近一条还没绑房源、也没被丢弃的视频（同一渠道同一发送人）。"""
    clauses = ["status='pending'"]
    params: list = []
    if channel:
        clauses.append("channel=?")
        params.append(channel)
    if sender:
        clauses.append("sender=?")
        params.append(sender)
    with transaction() as connection:
        row = connection.execute(
            f"SELECT * FROM channel_clips WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
    return _row_to_clip(row)


def list_clips(channel: str = "", status: str = "", limit: int = 20) -> list[dict]:
    clauses, params = [], []
    if channel:
        clauses.append("channel=?")
        params.append(channel)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with transaction() as connection:
        rows = connection.execute(
            f"SELECT * FROM channel_clips{where} ORDER BY created_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    return [_row_to_clip(row) for row in rows]


def drop_clip(clip_id: str) -> bool:
    with transaction() as connection:
        row = connection.execute(
            "SELECT status FROM channel_clips WHERE id=?", (clip_id,)
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "UPDATE channel_clips SET status='dropped', updated_at=? WHERE id=?",
            (utc_now(), clip_id),
        )
    # 取消就把占地方的原始视频也删掉，避免 data/inbox 越来越肥
    shutil.rmtree(CLIP_DIR / clip_id, ignore_errors=True)
    return True


def _candidate(index: int, row) -> dict:
    return {
        "index": index,
        "property_id": row["id"],
        "label": describe_property(row),
        "match_key": row["match_key"],
        "status": row["status"],
    }


def resolve(query: str, limit: int = 5) -> dict:
    """只做解析，不落任何数据，方便先给用户看候选。"""
    result = resolve_property(query, limit=limit)
    if result.kind == "matched":
        return {
            "status": "matched",
            "property": _candidate(0, result.property),
            "candidates": [],
        }
    if result.kind == "ambiguous":
        return {
            "status": "ambiguous",
            "property": None,
            "candidates": [_candidate(i + 1, row) for i, row in enumerate(result.candidates)],
        }
    return {"status": "none", "property": None, "candidates": [], "hint": result.hint}


def bind_clip(
    clip_id: str,
    *,
    query: str = "",
    property_id: str = "",
    target_duration: int = 45,
    aspect_ratio: str = "9:16",
    voice: str = "female_warm",
    style: str = "professional",
) -> dict:
    """把待绑定视频落到具体房源上并建任务。

    优先用 property_id（用户在候选里选了第几条），否则按 query 解析。
    解析有歧义时不动数据，把候选原样返回。
    """
    clip = get_clip(clip_id)
    if not clip:
        raise ChannelError("视频不存在，请重新发送")
    if clip["status"] == "bound" and clip["task_id"]:
        return {"status": "already_bound", "task": serialize_task(get_task(clip["task_id"])), "clip": clip}
    if clip["status"] == "dropped":
        raise ChannelError("这段视频已被取消，请重新发送")

    with transaction() as connection:
        if property_id:
            property_row = connection.execute(
                "SELECT * FROM properties WHERE id=?", (property_id,)
            ).fetchone()
            if property_row is None:
                raise ChannelError("选择的房源不存在")
            resolved = {
                "status": "matched",
                "property": _candidate(0, property_row),
                "candidates": [],
            }
        else:
            property_row = None
            resolved = None

    if resolved is None:
        if not query.strip():
            raise ChannelError("请提供房源信息，例如：A区 中关村大厦 1201")
        resolved = resolve(query.strip())
        if resolved["status"] != "matched":
            return {**resolved, "clip": clip}
        with transaction() as connection:
            property_row = connection.execute(
                "SELECT * FROM properties WHERE id=?", (resolved["property"]["property_id"],)
            ).fetchone()

    # 视频要搬进任务目录，和 WebUI 上传的路径结构保持一致
    task_no = new_task_no()
    task_dir = UPLOAD_DIR / task_no
    task_dir.mkdir(parents=True, exist_ok=True)
    source = Path(_stored_path(clip_id))
    if not source.is_file():
        raise ChannelError("原始视频文件已丢失，请重新发送")
    target = task_dir / source.name
    try:
        shutil.move(str(source), str(target))
    except OSError:
        shutil.copy2(source, target)

    task_id = create_task_record(
        property_row=property_row,
        video_path=target,
        original_filename=clip["filename"] or target.name,
        target_duration=target_duration,
        aspect_ratio=aspect_ratio,
        voice=voice,
        style=style,
        source=clip["channel"] or "desktop",
        task_no=task_no,
    )
    with transaction() as connection:
        connection.execute(
            """UPDATE channel_clips SET status='bound', task_id=?, property_id=?,
               stored_path='', updated_at=? WHERE id=?""",
            (task_id, property_row["id"], utc_now(), clip_id),
        )
    shutil.rmtree(CLIP_DIR / clip_id, ignore_errors=True)
    return {
        "status": "bound",
        "task": serialize_task(get_task(task_id)),
        "property": resolved["property"],
        "clip": get_clip(clip_id),
    }


def _stored_path(clip_id: str) -> str:
    with transaction() as connection:
        row = connection.execute(
            "SELECT stored_path FROM channel_clips WHERE id=?", (clip_id,)
        ).fetchone()
    return row["stored_path"] if row else ""
