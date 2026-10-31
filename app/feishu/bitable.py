"""飞书多维表格同步（尽力而为，不阻塞主流程）。

多维表格是朋友日常管理的“业务面板”，我们只推进任务维度的记录，
字段名在 message 里给出，表结构不匹配时只记日志、不抛异常——
因为出片这件事不能被一张表的字段改名卡住。
"""

from __future__ import annotations

import logging

from .client import FeishuClient, FeishuConfig, FeishuError

logger = logging.getLogger(__name__)

TASK_NO_FIELD = "任务编号"


def bitable_client() -> FeishuClient | None:
    """建一个只用来写表的客户端。

    写多维表格走的是 REST + tenant_access_token，**不需要事件订阅**，
    所以即使消息入口用的是 WorkBuddy 的飞书通道（方案 A），
    也可以单独建一个只管表格权限的应用来写，两边不会抢长连接。
    """
    config = FeishuConfig.from_env()
    return FeishuClient(config) if config.has_bitable else None


def _fields_for_task(task) -> dict:
    payload = {
        "任务编号": task["task_no"],
        "房源": " ".join(
            part for part in (task["region_norm"], task["building_norm"], task["room_norm"]) if part
        ),
        "面积": task["area_raw"],
        "状态": task["status"],
        "进度": int(task["progress"] or 0),
        "来源": task["source"],
        "口播文案": task["script"],
        "更新时间": task["updated_at"],
    }
    if task["output_video_path"]:
        payload["成片路径"] = task["output_video_path"]
    if task["error_message"]:
        payload["错误信息"] = task["error_message"][:500]
    return {key: value for key, value in payload.items() if value not in ("", None)}


def sync_task(client: FeishuClient, task) -> str:
    """有则更新、无则新建，返回 ok / skipped 便于在状态接口里展示。"""
    if not client.config.has_bitable:
        return "skipped"
    table_id = client.config.task_table_id
    fields = _fields_for_task(task)
    try:
        records = client.bitable_search(table_id, TASK_NO_FIELD, task["task_no"])
        if records:
            record_id = records[0].get("record_id")
            if record_id:
                client.bitable_update_record(table_id, record_id, fields)
                return "updated"
        # 首次写入时去掉只读/可能不被识别的空字段
        client.bitable_create_record(table_id, fields)
        return "created"
    except FeishuError as error:
        logger.warning("多维表格同步失败（任务 %s）：%s", task["task_no"], error)
        return "failed"
