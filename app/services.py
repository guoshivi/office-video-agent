import json
import random
import re
import string
from datetime import datetime

from .db import EXPORT_DIR, get_settings, json_dumps, transaction, utc_now

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


def get_task(task_id: str):
    with transaction() as connection:
        row = connection.execute(
            """SELECT t.*, p.region_norm, p.building_norm, p.room_norm,
                      p.area_raw, p.rent_raw, p.status AS property_status
               FROM tasks t JOIN properties p ON p.id=t.property_id
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
