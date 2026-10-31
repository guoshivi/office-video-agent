"""多维表格桥接：把审核队列镜像到飞书，并把人那边改的结论拉回来。

为什么是镜像而不是主库：
朋友在外面用手机审、在电脑上用 WebUI 审，两边都要能操作同一条队列。
但飞书表格是最容易抽风的一环（字段改名、权限、网络），
所以**本地 SQLite 始终是权威**，表格只是它的一个视图。

两个方向：
- push：本地队列 → 表格一行（任务编号为键，有则更新）
- pull：表格「审核状态」被改成通过/驳回 → 回写本地，并按设置决定是否自动发

没有飞书凭据时整个模块静默不工作，审核台照常能用。
"""

from __future__ import annotations

import logging
import threading
import time

from ..db import get_settings
from ..services import get_task
from . import service, store

logger = logging.getLogger(__name__)

FIELD_TASK_NO = "任务编号"
FIELD_PROPERTY = "房源"
FIELD_AREA = "面积"
FIELD_SCRIPT = "口播文案"
FIELD_VERDICT = "预检结论"
FIELD_STATUS = "审核状态"
FIELD_REVIEWER = "审核人"
FIELD_REASON = "驳回原因"

STATUS_TO_LOCAL = {"通过": "approved", "驳回": "rejected", "已发布": "published"}
LOCAL_TO_STATUS = {"pending": "待审", "approved": "通过", "rejected": "驳回", "published": "已发布"}

_watcher: threading.Thread | None = None
_stop = threading.Event()


def _client():
    from ..feishu.bitable import bitable_client

    client = bitable_client()
    if client is None or not client.config.has_bitable:
        return None
    return client


def available() -> bool:
    """有没有配好多维表格。没配时整个桥接是静默失效的，不是错误。"""
    try:
        return _client() is not None
    except Exception:  # noqa: BLE001 - 凭据读取失败同样按"不可用"处理
        return False


def push_item(task_id: str) -> str:
    """把一条审核记录写进多维表格。返回 created / updated / skipped / failed。"""
    client = _client()
    if client is None:
        return "skipped"

    item = store.detail(task_id)
    if item is None:
        return "skipped"
    task = get_task(task_id)
    if task is None:
        return "skipped"

    precheck_summary = ""
    try:
        import json

        payload = json.loads(item.get("precheck_json") or "{}")
        precheck_summary = payload.get("summary", "")
    except json.JSONDecodeError:
        precheck_summary = ""

    fields = {
        FIELD_TASK_NO: item["task_no"],
        FIELD_PROPERTY: " ".join(
            part for part in (item.get("region_norm"), item.get("building_norm"), item.get("room_norm")) if part
        ),
        FIELD_AREA: item.get("area_raw") or "",
        FIELD_SCRIPT: (item.get("script") or "")[:2000],
        FIELD_VERDICT: precheck_summary,
        FIELD_STATUS: LOCAL_TO_STATUS.get(item["status"], "待审"),
        FIELD_REVIEWER: item.get("reviewer") or "",
        FIELD_REASON: item.get("note") or "",
    }
    fields = {key: value for key, value in fields.items() if value not in ("", None)}

    table_id = client.config.task_table_id
    try:
        records = client.bitable_search(table_id, FIELD_TASK_NO, item["task_no"])
        if records:
            record_id = records[0].get("record_id")
            if record_id:
                client.bitable_update_record(table_id, record_id, fields)
                return "updated"
        client.bitable_create_record(table_id, fields)
        return "created"
    except Exception as error:  # noqa: BLE001 - 表格是镜像，任何问题都不该阻塞本地
        logger.warning("推送多维表格失败（%s）：%s", item["task_no"], error)
        return "failed"


def push_all(limit: int = 50) -> dict:
    result = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
    for item in store.list_items(limit=limit):
        result[push_item(item["task_id"])] += 1
    return result


def pull_decisions() -> dict:
    """把表格里被改过的审核状态同步回本地。

    只处理本地还是 pending 的记录——已经裁决过的不覆盖，
    否则人在 WebUI 上改完会被表格里的旧值盖回去。
    """
    client = _client()
    if client is None:
        return {"available": False, "approved": 0, "rejected": 0}

    table_id = client.config.task_table_id
    applied = {"available": True, "approved": 0, "rejected": 0, "skipped": 0}
    try:
        records = [
            record
            for status in ("通过", "驳回")
            for record in _records_with_status(client, table_id, status)
        ]
    except Exception as error:  # noqa: BLE001
        logger.warning("读取多维表格失败：%s", error)
        return {"available": False, "approved": 0, "rejected": 0, "error": str(error)}

    for record in records:
        fields = record.get("fields") or {}
        task_no = _field_text(fields.get(FIELD_TASK_NO))
        status = _field_text(fields.get(FIELD_STATUS))
        if not task_no:
            continue
        task_id = _find_task_id(task_no)
        if not task_id:
            continue
        local = store.get(task_id)
        if local is None or local["status"] != "pending":
            applied["skipped"] += 1
            continue

        reviewer = _field_text(fields.get(FIELD_REVIEWER))
        reason = _field_text(fields.get(FIELD_REASON))
        try:
            if STATUS_TO_LOCAL.get(status) == "approved":
                service.approve(task_id, reviewer=reviewer, note=reason or "多维表格审核通过")
                applied["approved"] += 1
            else:
                # 表格里驳回没填原因时给个占位，别让本地流程卡在"没原因"上
                service.reject(task_id, reviewer=reviewer, note=reason or "多维表格驳回")
                applied["rejected"] += 1
        except service.ReviewError as error:
            logger.warning("应用表格裁决失败（%s）：%s", task_no, error)
            applied["skipped"] += 1
    return applied


def _records_with_status(client, table_id: str, status: str) -> list[dict]:
    """取某个审核状态下的**全部**记录。

    优先用带翻页的 bitable_search_all：只取第一页 20 条的话，
    表格里标成"通过"的记录一多，第 21 条之后就永远同步不回来了。
    """
    search_all = getattr(client, "bitable_search_all", None)
    if search_all is not None:
        return search_all(table_id, FIELD_STATUS, status)
    return client.bitable_search(table_id, FIELD_STATUS, status)


def _field_text(value) -> str:
    """多维表格的字段可能是字符串、列表或富文本对象，统一取成文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(_field_text(item) for item in value).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    return str(value).strip()


def _find_task_id(task_no: str) -> str:
    from ..db import transaction

    with transaction() as connection:
        row = connection.execute("SELECT id FROM tasks WHERE task_no = ?", (task_no,)).fetchone()
    return row["id"] if row else ""


# ------------------------------------------------------------------ 后台轮询

def start_watcher(interval: int = 30) -> bool:
    """定时把表格里的人工裁决拉回来。

    为什么不接 webhook：飞书多维表格的自动化流程能发 HTTP 请求，
    但需要有公网可达地址，而朋友那台机器在内网。轮询 30 秒的延迟
    对"审完再发"这件事完全够用，也跟当初"多维表格不订阅事件"的决定一致。
    """
    global _watcher

    settings = get_settings()
    if settings.get("review_bitable_sync") != "1":
        return False
    if _watcher is not None and _watcher.is_alive():
        return True

    _stop.clear()

    def loop() -> None:
        while not _stop.is_set():
            try:
                result = pull_decisions()
                if result.get("approved") or result.get("rejected"):
                    logger.info("多维表格同步：%s", result)
            except Exception as error:  # noqa: BLE001 - 轮询线程不能死
                logger.warning("多维表格轮询异常：%s", error)
            _stop.wait(interval)

    _watcher = threading.Thread(target=loop, name="bitable-review-sync", daemon=True)
    _watcher.start()
    logger.info("多维表格审核轮询已启动（%s 秒）", interval)
    return True


def stop_watcher() -> None:
    _stop.set()
