"""发布层统一入口。

原则和 bitable 一致：发布是"尽力而为"，不能把出片主流程拖下水，
但和 bitable 不同的是——发布是**破坏性动作**，所以失败必须如实回报，
不允许静默吞掉，也不允许把"读不到结果"当成成功。

三个平台差异都收敛在这一层：
- 小红书走 MCP（服务已封装登录与风控）；
- 抖音走 CDP（附加真实 Chrome）；
- 文案长度、违禁词、频率限制在 policy 里统一处理。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import DATA_DIR, get_settings, json_dumps, transaction, utc_now
from . import policy
from .douyin import DouyinPublisher, chrome_launch_hint
from .xhs import PublishResult, XhsPublisher

logger = logging.getLogger(__name__)

PLATFORM_LABELS = {"xiaohongshu": "小红书", "douyin": "抖音"}
STAGING_DIR = DATA_DIR / policy.STAGING_SUBDIR
QRCODE_DIR = DATA_DIR / "publish"


# ---------- 文案组装 ----------

def _area_text(task) -> str:
    sqm = task["area_sqm"] if "area_sqm" in task.keys() else None
    if sqm:
        return f"{int(round(float(sqm)))}平"
    raw = (task["area_raw"] or "").strip()
    if not raw:
        return ""
    return raw if any(unit in raw for unit in ("平", "㎡", "m²")) else f"{raw}平"


def short_title(task, limit: int = policy.XHS_TITLE_MAX) -> str:
    """把房源压成一个能当标题的短句。

    小红书标题 20 字是硬顶，所以按"信息密度高的先留"排优先级：
    区域 → 楼栋 → 面积 → 房号 → 格局，超长就砍尾巴而不是拦腰截断。
    """
    region = (task["region_norm"] or "").strip()
    building = (task["building_norm"] or "").strip()
    room = (task["room_norm"] or "").strip()
    area = _area_text(task)
    partition = (task["partition_raw"] or "").strip()

    def join(*items: str) -> str:
        return " ".join(item for item in items if item)

    candidates = [
        join(region, building, area, room),
        join(region, building, area),
        join(building, area, room),
        join(building, area),
        join(area, room),
    ]
    for text in candidates:
        if text and len(text) <= limit:
            return text
    fallback = next((item for item in candidates if item), "")
    return fallback[:limit]


def build_post(task) -> tuple[str, str, list[str]]:
    """返回 (标题, 正文, 话题标签)。"""
    settings = get_settings()
    title = short_title(task)

    body = (task["script"] or "").strip()
    contact = (settings.get("public_contact") or "").strip()
    if contact and contact not in body:
        body = f"{body}\n\n{contact}" if body else contact

    tags: list[str] = ["办公室出租"]
    for value in ((task["region_norm"] or "").strip(), (task["building_norm"] or "").strip()):
        if value and value not in tags:
            tags.append(value)
    return title, body.strip(), tags[:5]


# ---------- 频率控制 ----------

def quota_state(platform: str, settings: dict | None = None) -> dict:
    """返回今日已发条数与是否允许继续发。

    中介号不是矩阵号，宁可少发也不要进风控。上限和最小间隔都能在设置里改。
    """
    settings = settings or get_settings()
    try:
        daily_limit = max(1, int(settings.get("publish_daily_limit") or 3))
    except ValueError:
        daily_limit = 3
    try:
        min_interval = max(0, int(settings.get("publish_min_interval_minutes") or 60))
    except ValueError:
        min_interval = 60

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with transaction() as connection:
        rows = connection.execute(
            "SELECT created_at FROM publish_records "
            "WHERE platform = ? AND status IN ('published','uncertain') AND created_at >= ? "
            "ORDER BY created_at DESC",
            (platform, since),
        ).fetchall()

    used = len(rows)
    allowed = used < daily_limit
    wait_minutes = 0
    if allowed and rows and min_interval:
        last = datetime.fromisoformat(rows[0]["created_at"])
        ready_at = last + timedelta(minutes=min_interval)
        remaining = (ready_at - datetime.now(timezone.utc)).total_seconds() / 60
        if remaining > 0:
            allowed = False
            wait_minutes = int(remaining) + 1
    return {
        "used_today": used,
        "daily_limit": daily_limit,
        "allowed": allowed,
        "wait_minutes": wait_minutes,
    }


# ---------- 记录 ----------

def _record_id() -> str:
    return uuid.uuid4().hex[:16]


def get_record(task_id: str, platform: str):
    with transaction() as connection:
        return connection.execute(
            "SELECT * FROM publish_records WHERE task_id = ? AND platform = ?",
            (task_id, platform),
        ).fetchone()


def list_records(limit: int = 50) -> list[dict]:
    with transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM publish_records ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def _upsert_record(task_id: str, platform: str, payload: dict) -> None:
    now = utc_now()
    with transaction() as connection:
        existing = connection.execute(
            "SELECT id FROM publish_records WHERE task_id = ? AND platform = ?",
            (task_id, platform),
        ).fetchone()
        if existing:
            assignments = ", ".join(f"{key} = ?" for key in payload)
            connection.execute(
                f"UPDATE publish_records SET {assignments}, updated_at = ? WHERE id = ?",
                (*payload.values(), now, existing["id"]),
            )
        else:
            columns = ", ".join(payload.keys())
            placeholders = ", ".join("?" for _ in payload)
            connection.execute(
                f"INSERT INTO publish_records (id, task_id, platform, created_at, updated_at, "
                f"{columns}) VALUES (?, ?, ?, ?, ?, {placeholders})",
                (_record_id(), task_id, platform, now, now, *payload.values()),
            )


# ---------- 发布 ----------

def publishers(settings: dict | None = None) -> dict:
    settings = settings or get_settings()
    return {
        "xiaohongshu": XhsPublisher(
            settings.get("publish_xhs_mcp_url", ""), settings.get("publish_xhs_token", "")
        ),
        "douyin": DouyinPublisher(settings.get("publish_douyin_cdp_url", "")),
    }


def platform_status(settings: dict | None = None) -> dict:
    settings = settings or get_settings()
    clients = publishers(settings)
    douyin_status = clients["douyin"].status()
    return {
        "xiaohongshu": {
            "enabled": settings.get("publish_xhs_enabled") == "1",
            **clients["xiaohongshu"].status(),
            "quota": quota_state("xiaohongshu", settings),
        },
        "douyin": {
            "enabled": settings.get("publish_douyin_enabled") == "1",
            **douyin_status.as_dict(),
            "chrome_hint": chrome_launch_hint(DATA_DIR / "chrome-profile"),
            "quota": quota_state("douyin", settings),
        },
    }


def publish_task(task, platforms: list[str] | None = None, force: bool = False) -> dict:
    """把一条已出片的任务发布到指定平台。

    force：跳过"已发布过"的拦截，用于人工确认后重发。
    """
    settings = get_settings()
    platforms = platforms or [
        name
        for name, key in (("xiaohongshu", "publish_xhs_enabled"), ("douyin", "publish_douyin_enabled"))
        if settings.get(key) == "1"
    ]
    if not platforms:
        return {"ok": False, "message": "没有启用任何发布平台", "results": {}}

    # 注意 `Path("")` 等于当前目录且 exists() 为真，这里必须判空并只认文件，
    # 否则"没成片"会被误判成有视频，后面复制目录时才炸。
    raw_path = (task["output_video_path"] or "").strip()
    source = Path(raw_path) if raw_path else None
    if source is None or not source.is_file():
        return {
            "ok": False,
            "message": "成片不存在，先出片再发布",
            "results": {},
        }

    # 统一 stage 到 ASCII 短路径：规避小红书对中文路径的不稳定，
    # 也避免上传期间原文件被改写。
    staged = policy.stage_video(source, STAGING_DIR, task["task_no"])
    title, content, tags = build_post(task)
    clients = publishers(settings)

    results: dict[str, dict] = {}
    for platform in platforms:
        label = PLATFORM_LABELS.get(platform, platform)
        if platform not in PLATFORM_LABELS:
            results[platform] = {"ok": False, "message": f"未知平台 {platform}"}
            continue

        check = policy.check_xhs(title, content, staged)
        if not check.ok:
            results[platform] = {
                "ok": False,
                "message": f"{label}未通过规则校验：" + "；".join(i.message for i in check.blocking),
                "issues": check.as_dict()["issues"],
            }
            continue

        previous = get_record(task["id"], platform)
        if previous and previous["status"] in ("published", "uncertain") and not force:
            results[platform] = {
                "ok": True,
                "message": f"{label}已发布过，跳过（如需重发请用 force）",
                "remote_url": previous["remote_url"],
            }
            continue

        quota = quota_state(platform, settings)
        if not quota["allowed"]:
            reason = (
                f"今日已发 {quota['used_today']}/{quota['daily_limit']} 条"
                if quota["wait_minutes"] == 0
                else f"距上次发布不足间隔，还需等 {quota['wait_minutes']} 分钟"
            )
            results[platform] = {"ok": False, "message": f"{label}暂不发布：{reason}"}
            continue

        _upsert_record(
            task["id"],
            platform,
            {
                "status": "publishing",
                "title": check.title,
                "content": check.content,
                "tags_json": json_dumps(tags),
                "video_path": str(staged),
                "message": "",
                "attempts": (previous["attempts"] + 1) if previous else 1,
            },
        )

        if platform == "xiaohongshu":
            outcome = clients["xiaohongshu"].publish_video(
                title=check.title, content=check.content, video=staged, tags=tags
            )
        else:
            outcome = clients["douyin"].publish_video(
                title=check.title,
                content=check.content,
                video=staged,
                tags=tags,
                screenshot_dir=STAGING_DIR,
                stem=task["task_no"],
            )

        status = "failed"
        if outcome.ok:
            status = "uncertain" if outcome.uncertain else "published"
        _upsert_record(
            task["id"],
            platform,
            {
                "status": status,
                "remote_id": outcome.remote_id,
                "remote_url": outcome.remote_url,
                "message": outcome.message[:500],
            },
        )
        results[platform] = {**outcome.as_dict(), "issues": check.as_dict()["issues"]}

    return {
        "ok": any(item.get("ok") for item in results.values()),
        "title": title,
        "results": results,
    }


def publish_qrcode(platform: str = "xiaohongshu") -> dict:
    """取扫码登录二维码（目前只有小红书需要）。"""
    if platform != "xiaohongshu":
        status = DouyinPublisher(get_settings().get("publish_douyin_cdp_url", "")).status()
        return {"ok": False, "message": status.message}
    return XhsPublisher(
        get_settings().get("publish_xhs_mcp_url", ""),
        get_settings().get("publish_xhs_token", ""),
    ).login_qrcode(QRCODE_DIR)


__all__ = [
    "PLATFORM_LABELS",
    "PublishResult",
    "build_post",
    "get_record",
    "list_records",
    "platform_status",
    "publish_qrcode",
    "publish_task",
    "publishers",
    "quota_state",
    "short_title",
]
