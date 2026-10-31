import json
import random
import re
import string
import uuid
from datetime import datetime

from .db import EXPORT_DIR, get_settings, json_dumps, transaction, utc_now

TASK_SELECT = """t.*, p.region_norm, p.building_norm, p.room_norm,
    p.area_raw, p.area_sqm, p.rent_raw, p.partition_raw, p.orientation_raw,
    p.furnishing_layout_raw, p.status AS property_status"""

SENSITIVE_PATTERN = re.compile(r"密码|门禁|钥匙|佣金|中介费|底价|红包|电话|手机|微信|议价", re.I)


def safe_public_text(value: str, limit: int = 60) -> str:
    value = re.sub(r"\s+", " ", (value or "")).strip()
    if not value or SENSITIVE_PATTERN.search(value):
        return ""
    return value[:limit]


def public_property(row) -> dict:
    return {
        "id": row["id"],
        "match_key": row["match_key"],
        "region": row["region_norm"],
        "building": row["building_norm"],
        "room": row["room_norm"],
        "area": row["area_raw"],
        "area_sqm": row["area_sqm"],
        "partition": row["partition_raw"],
        "rent": row["rent_raw"],
        "orientation": row["orientation_raw"],
        "furnishing_layout": row["furnishing_layout_raw"],
        "publish_date": row["publish_date_raw"],
        "source_sheet": row["source_sheet"],
        "source_row": row["source_row"],
        "is_new_marked": bool(row["is_new_marked"]),
        "is_special_marked": bool(row["is_special_marked"]),
        "status": row["status"],
        "updated_at": row["updated_at"],
    }


def generate_script(property_row) -> tuple[str, list[str]]:
    settings = get_settings()
    region = safe_public_text(property_row["region_norm"], 30)
    building = safe_public_text(property_row["building_norm"], 30)
    area = safe_public_text(property_row["area_raw"], 20)
    partition = safe_public_text(property_row["partition_raw"], 20)
    orientation = safe_public_text(property_row["orientation_raw"], 30)
    furnishing = safe_public_text(property_row["furnishing_layout_raw"], 60)

    location = "、".join(part for part in (region, building) if part) or "当前在租"
    opening = f"正在找办公室的朋友，今天看一套位于{location}的房源。"
    facts = []
    if area:
        facts.append(f"面积信息为{area}")
    if partition:
        facts.append(f"格局为{partition}")
    if orientation:
        facts.append(f"朝向{orientation}")
    if furnishing:
        facts.append(f"房源特点包括{furnishing}")
    if not facts:
        facts.append("具体面积、格局和装修信息，需要在发布前进一步确认")

    contact = settings.get("public_contact") or "欢迎私信咨询房源详情和看房安排"
    ending = f"租金和物业费口径请以最新房源确认为准。{contact}。"
    script = opening + "，".join(facts) + "。" + ending
    warnings = ["这是基于房源字段生成的口播草稿，尚未经过视频画面分析或人工事实核验。"]
    if property_row["rent_raw"]:
        warnings.append("原表租金口径可能不统一，草稿未直接播报租金，请确认后再补充。")
    if property_row["status"] == "needs_review":
        warnings.append("该房源存在重复或缺失匹配字段，生成前需要人工确认。")
    return script, warnings


def new_task_no() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"V{datetime.now():%Y%m%d}-{suffix}"


def create_task_record(
    *,
    property_row,
    video_path,
    original_filename: str,
    target_duration: int = 45,
    aspect_ratio: str = "9:16",
    voice: str = "female_warm",
    style: str = "professional",
    source: str = "webui",
    notify_chat_id: str = "",
    task_no: str = "",
) -> str:
    """WebUI 与飞书共用的唯一建任务入口，避免两个入口各写一套状态机。"""
    script, warnings = generate_script(property_row)
    task_id = str(uuid.uuid4())
    now = utc_now()
    with transaction() as connection:
        connection.execute(
            """INSERT INTO tasks(
                id, task_no, property_id, source, original_filename, source_video_path,
                target_duration, aspect_ratio, voice, style, script, warnings_json,
                status, notify_chat_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'script_ready', ?, ?, ?)""",
            (
                task_id, task_no or new_task_no(), property_row["id"], source,
                original_filename, str(video_path), target_duration, aspect_ratio,
                voice, style, script, json_dumps(warnings), notify_chat_id, now, now,
            ),
        )
    return task_id


def set_task_script(task_id: str, script: str) -> bool:
    script = script.strip()
    if not script:
        return False
    with transaction() as connection:
        cursor = connection.execute(
            """UPDATE tasks SET script=?, status='script_ready',
               workbench_package_path='', updated_at=? WHERE id=?""",
            (script, utc_now(), task_id),
        )
        return cursor.rowcount > 0


def find_task_by_no(task_no: str):
    with transaction() as connection:
        return connection.execute(
            f"""SELECT {TASK_SELECT} FROM tasks t JOIN properties p ON p.id=t.property_id
               WHERE UPPER(t.task_no)=UPPER(?)""",
            (task_no.strip(),),
        ).fetchone()


STATUS_LABELS = {
    "received": "已接收",
    "script_ready": "口播待确认",
    "exported": "已导出任务包",
    "processing": "出片中",
    "review": "待审片",
    "completed": "已完成",
    "failed": "失败",
}


def _property_line(row) -> str:
    parts = [row["region_norm"], row["building_norm"], row["room_norm"]]
    label = " ".join(part for part in parts if part) or "未命名房源"
    extras = [
        value for value in (
            f"{row['area_raw']}" if row["area_raw"] else "",
            row["partition_raw"],
            f"朝{row['orientation_raw']}" if row["orientation_raw"] else "",
            row["furnishing_layout_raw"],
        ) if value
    ]
    return f"{label}\n{'｜'.join(extras)}" if extras else label


def format_task_message(task, settings: dict, brief: bool = False) -> str:
    """给飞书用的纯文本卡片，尽量一行一个信息，手机上好读。"""
    label = STATUS_LABELS.get(task["status"], task["status"])
    progress = int(task["progress"] or 0)
    header = f"{task['task_no']} · {label}"
    lines = [header, _property_line(task)]

    if brief:
        if task["stage"]:
            lines.append(f"进度：{progress}% {task['stage']}")
        if task["error_message"]:
            lines.append(f"原因：{task['error_message']}")
        if task["output_video_path"]:
            lines.append(f"成片：{task['output_video_path']}")
        lines.append(f"口播：{task['script']}")
        return "\n".join(lines)

    lines.append("")
    lines.append("口播草稿：")
    lines.append(f"「{task['script']}」")
    warnings = json.loads(task["warnings_json"] or "[]")
    if warnings:
        lines.append("")
        lines.append("注意：" + warnings[0])
    lines.append("")
    lines.append(f"回复「出片 {task['task_no']}」开始生成")
    lines.append(f"回复「改口播 {task['task_no']} 新文案」修改口播")
    lines.append(f"回复「状态 {task['task_no']}」查看进度")
    return "\n".join(lines)


def get_task(task_id: str):
    with transaction() as connection:
        row = connection.execute(
            f"""SELECT {TASK_SELECT} FROM tasks t JOIN properties p ON p.id=t.property_id
               WHERE t.id=?""",
            (task_id,),
        ).fetchone()
    return row


def serialize_task(row, include_script: bool = True) -> dict:
    result = {
        "id": row["id"],
        "task_no": row["task_no"],
        "property_id": row["property_id"],
        "property_label": " ".join(
            part for part in (row["region_norm"], row["building_norm"], row["room_norm"]) if part
        ),
        "area": row["area_raw"],
        "rent": row["rent_raw"],
        "property_status": row["property_status"],
        "source": row["source"],
        "original_filename": row["original_filename"],
        "target_duration": row["target_duration"],
        "aspect_ratio": row["aspect_ratio"],
        "voice": row["voice"],
        "style": row["style"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "warnings": json.loads(row["warnings_json"] or "[]"),
        "workbench_package_path": row["workbench_package_path"],
        "output_video_path": row["output_video_path"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_script:
        result["script"] = row["script"]
    return result


def export_workbench_package(task_id: str) -> dict:
    task = get_task(task_id)
    if task is None:
        raise KeyError("任务不存在")
    with transaction() as connection:
        property_row = connection.execute(
            "SELECT * FROM properties WHERE id=?", (task["property_id"],)
        ).fetchone()
        settings = get_settings()

    package_dir = EXPORT_DIR / task["task_no"]
    package_dir.mkdir(parents=True, exist_ok=True)
    script_path = package_dir / "script.txt"
    manifest_path = package_dir / "task.json"
    script_path.write_text(task["script"], encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "task_id": task["id"],
        "task_no": task["task_no"],
        "created_at": utc_now(),
        "source_video": task["source_video_path"],
        "original_filename": task["original_filename"],
        "property": public_property(property_row),
        "render": {
            "target_duration": task["target_duration"],
            "aspect_ratio": task["aspect_ratio"],
            "voice": task["voice"],
            "style": task["style"],
        },
        "script_file": str(script_path),
        "warnings": json.loads(task["warnings_json"] or "[]"),
        "configured_workbench_path": settings.get("workbench_path", ""),
        "note": "This is an export package, not a rendered video. Connect the existing workbench adapter to process it.",
    }
    manifest_path.write_text(json_dumps(manifest), encoding="utf-8")
    with transaction() as connection:
        connection.execute(
            "UPDATE tasks SET status='exported', workbench_package_path=?, updated_at=? WHERE id=?",
            (str(package_dir), utc_now(), task_id),
        )
    return {"package_path": str(package_dir), "manifest_path": str(manifest_path)}
