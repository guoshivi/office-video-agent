"""审核台接口。

一条片子的命运在这几个接口里被决定：待审 → 通过 / 驳回 → 发布。
"通过"默认会被预检拦截——预检有 blocking 时必须显式 confirm_risky，
这样误报可以越过，但一定会留下"人工确认过"的记录。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..review import service
from ..review.service import ReviewError

router = APIRouter(prefix="/api/review", tags=["review"])


class DecisionRequest(BaseModel):
    reviewer: str = Field(default="", description="审核人，留空视为本机操作者")
    note: str = Field(default="", description="备注；驳回时必填")
    confirm_risky: bool = Field(default=False, description="预检有 blocking 时强行通过")
    platforms: list[str] | None = Field(default=None, description="通过后立即发布的平台")


@router.get("/items")
def list_items(
    status: str | None = Query(None, description="pending / approved / rejected / published"),
    limit: int = Query(50, ge=1, le=200),
    refresh: bool = Query(False, description="先把已出片未入队的任务补进队列并补跑预检"),
):
    if refresh:
        service.refresh_queue(auto_precheck=True)
    return service.list_queue(status=status, limit=limit)


@router.get("/counts")
def review_counts():
    from ..review import store

    return store.counts()


@router.post("/refresh")
def refresh():
    return service.refresh_queue(auto_precheck=True)


# ------------------------------------------------------------------ 多维表格桥接
# 放在 /{task_id} 之前注册，避免路径参数把 /bitable 吃掉。
# 桥接是可选能力：没有飞书凭据时返回 available=False，审核台照常工作。

@router.get("/bitable/status")
def bitable_status():
    from ..db import get_settings
    from ..feishu.bitable import bitable_client
    from ..review import bitable_bridge

    client = bitable_client()
    settings = get_settings()
    return {
        "available": bitable_bridge.available(),
        "mirror_enabled": settings.get("review_bitable_sync") == "1",
        "table_id": client.config.task_table_id if client else "",
        "hint": "" if client else "未配置飞书多维表格凭据，审核队列仅在本机可用",
    }


@router.post("/bitable/push")
def bitable_push(task_id: str = Query("", description="留空则推送整个队列")):
    from ..review import bitable_bridge

    if not bitable_bridge.available():
        return {"available": False, "message": "未配置多维表格，跳过"}
    if task_id:
        return {"available": True, "result": bitable_bridge.push_item(task_id)}
    return {"available": True, "result": bitable_bridge.push_all()}


@router.post("/bitable/pull")
def bitable_pull():
    from ..review import bitable_bridge

    return bitable_bridge.pull_decisions()


@router.get("/{task_id}")
def review_detail(task_id: str):
    try:
        return service.detail(task_id)
    except ReviewError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/{task_id}/precheck")
def rerun_precheck(task_id: str):
    try:
        return service.run_precheck(task_id)
    except ReviewError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/{task_id}/approve")
def approve(task_id: str, payload: DecisionRequest | None = None):
    body = payload or DecisionRequest()
    try:
        return service.approve(
            task_id,
            reviewer=body.reviewer,
            note=body.note,
            confirm_risky=body.confirm_risky,
            platforms=body.platforms,
        )
    except ReviewError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/{task_id}/reject")
def reject(task_id: str, payload: DecisionRequest | None = None):
    body = payload or DecisionRequest()
    try:
        return service.reject(task_id, reviewer=body.reviewer, note=body.note)
    except ReviewError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/{task_id}/skip")
def skip(task_id: str, payload: DecisionRequest | None = None):
    body = payload or DecisionRequest()
    return service.skip(task_id, reason=body.note)
