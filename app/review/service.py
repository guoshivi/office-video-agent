"""审核台的编排层：把预检、队列、发布串成一条线。

这里是唯一知道"流程"的地方，store 只管存，publish 只管发。
换掉多维表格、换掉发布实现，都不影响这一层的语义。
"""

from __future__ import annotations

import json
import logging

from ..db import get_settings
from ..publish import publish_task
from ..services import get_task
from . import store
from .precheck import PrecheckReport, precheck

logger = logging.getLogger(__name__)

# 前端直接用这几个状态
STATUS_LABELS = {
    "pending": "待审",
    "approved": "已通过",
    "rejected": "已驳回",
    "published": "已发布",
    "skipped": "已跳过",
}


class ReviewError(RuntimeError):
    pass


def _task(task_id: str):
    row = get_task(task_id)
    if row is None:
        raise ReviewError("任务不存在")
    return row


def run_precheck(task_id: str) -> dict:
    """跑一次预检并存档。结果同时写回 review_items.level。"""
    row = _task(task_id)
    report: PrecheckReport = precheck(row)
    payload = report.as_dict()
    store.update_precheck(task_id, report.level, payload)
    return payload


def refresh_queue(auto_precheck: bool = True, limit: int = 20) -> dict:
    """补齐队列：已出片但没入队的任务补进来，缺预检的跑一遍。

    入库时不阻塞：只建队列行，预检按需跑，避免一次打开页面等十几秒。
    """
    created = store.missing_tasks(limit)
    for item in created:
        store.create(item["id"])

    checked = 0
    if auto_precheck:
        for item in store.list_items(limit=50):
            if item["status"] != "pending":
                continue
            payload = item.get("precheck_json") or "{}"
            if payload not in ("", "{}"):
                continue
            try:
                run_precheck(item["task_id"])
                checked += 1
            except Exception as error:  # noqa: BLE001 - 单条预检失败不该让整页打不开
                logger.warning("预检失败 %s：%s", item["task_no"], error)

    return {
        "enqueued": len(created),
        "prechecked": checked,
        "counts": store.counts(),
    }


def _decorate(item: dict) -> dict:
    payload = {}
    try:
        payload = json.loads(item.get("precheck_json") or "{}") or {}
    except json.JSONDecodeError:
        payload = {}
    checks = payload.get("checks", [])
    item["precheck"] = {
        "level": payload.get("level", item.get("level") or "unknown"),
        "summary": payload.get("summary", "还没做预检"),
        "blocking": [c for c in checks if c.get("level") == "blocking"],
        "warn": [c for c in checks if c.get("level") == "warn"],
        "ok": [c for c in checks if c.get("level") == "ok"],
    }
    item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
    item["property_label"] = " ".join(
        part
        for part in (item.get("region_norm"), item.get("building_norm"), item.get("room_norm"))
        if part
    )
    return item


def list_queue(status: str | None = None, limit: int = 50) -> dict:
    items = [_decorate(item) for item in store.list_items(status=status, limit=limit)]
    return {"items": items, "counts": store.counts()}


def detail(task_id: str) -> dict:
    item = store.detail(task_id)
    if item is None:
        store.create(task_id)
        item = store.detail(task_id)
    if item is None:
        raise ReviewError("任务不存在")
    return _decorate(item)


def approve(
    task_id: str,
    reviewer: str = "",
    note: str = "",
    confirm_risky: bool = False,
    platforms: list[str] | None = None,
) -> dict:
    """审核通过；按设置决定是否立刻发布。

    预检里只要有 blocking，默认拒绝通过——这是审核台存在的意义。
    误报时人工可以显式 confirm_risky 越过，但要留下记录。
    """
    item = detail(task_id)
    if item["status"] == "published":
        return {"ok": True, "status": "published", "message": "这条已经发布过了"}

    # 没预检就放行等于没有审核台，所以这里补一次再判断
    if item["precheck"]["level"] == "unknown":
        run_precheck(task_id)
        item = detail(task_id)

    level = item["precheck"]["level"]
    if level == "blocking" and not confirm_risky:
        reasons = "；".join(c["detail"] for c in item["precheck"]["blocking"][:3])
        raise ReviewError(f"预检有必须处理的问题：{reasons}。确认要强行通过请带 confirm_risky")

    store.set_status(
        task_id,
        "approved",
        reviewer=reviewer,
        note=note or ("预检有风险，人工确认后通过" if level == "blocking" else ""),
    )

    settings = get_settings()
    if settings.get("publish_auto_after_review") != "1":
        return {
            "ok": True,
            "status": "approved",
            "message": "已通过。自动发布是关闭的，可以在发布面板手动发。",
        }
    return publish_now(task_id, platforms)


def reject(task_id: str, reviewer: str = "", note: str = "") -> dict:
    if not (note or "").strip():
        raise ReviewError("驳回必须写原因，否则出片的人不知道该改什么")
    store.set_status(task_id, "rejected", reviewer=reviewer, note=note)
    return {"ok": True, "status": "rejected", "message": "已驳回，原因已记录"}


def publish_now(task_id: str, platforms: list[str] | None = None, force: bool = False) -> dict:
    row = _task(task_id)
    result = publish_task(row, platforms=platforms, force=force)
    if result.get("ok"):
        succeeded = [
            name for name, item in result["results"].items() if item.get("ok")
        ]
        store.set_status(
            task_id,
            "published",
            note="已发布：" + "、".join(succeeded),
        )
        result["status"] = "published"
    else:
        result["status"] = "approved"
    return result


def skip(task_id: str, reason: str = "") -> dict:
    store.set_status(task_id, "skipped", note=reason)
    return {"ok": True, "status": "skipped", "message": "已跳过"}
