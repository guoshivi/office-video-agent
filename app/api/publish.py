"""发布接口：查平台状态、取登录二维码、把成片推出去。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services import get_task

router = APIRouter(prefix="/api", tags=["publish"])


class PublishRequest(BaseModel):
    platforms: list[str] | None = Field(default=None, description="留空则用设置里已启用的平台")
    force: bool = Field(default=False, description="已发布过的任务是否强制重发")


@router.get("/publish/status")
def publish_platform_status():
    from ..publish import platform_status

    return platform_status()


@router.post("/tasks/{task_id}/publish")
def publish_task_endpoint(task_id: str, payload: PublishRequest | None = None):
    from ..publish import publish_task as run_publish

    row = get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    if not row["output_video_path"]:
        raise HTTPException(400, "任务还没有成片，先出片再发布")
    body = payload or PublishRequest()
    return run_publish(row, platforms=body.platforms, force=body.force)


@router.get("/publish/records")
def publish_records(limit: int = Query(50, ge=1, le=200)):
    from ..publish import list_records

    return {"items": list_records(limit)}


@router.post("/publish/login-qrcode")
def publish_login_qrcode(platform: str = Query("xiaohongshu")):
    """取扫码登录二维码。小红书需要扫码；抖音用 CDP 附加，返回启动提示。"""
    from ..publish import publish_qrcode

    return publish_qrcode(platform)
