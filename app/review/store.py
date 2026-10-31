"""审核队列的持久化层。

这里只做读写，不做编排——"通过后要不要自动发"属于业务决策，
放在 service.py，避免存储层反过来依赖发布层。
"""

from __future__ import annotations

import uuid

from ..db import json_dumps, transaction, utc_now

# 列表里要带上任务和房源的展示信息，审核的人不用再点进去才知道是哪套
_LIST_SQL = """
SELECT r.id, r.task_id, r.status, r.level, r.precheck_json,
       r.reviewer, r.note, r.decided_at, r.created_at, r.updated_at,
       t.task_no, t.status AS task_status, t.script, t.output_video_path,
       t.target_duration, t.source,
       p.region_norm, p.building_norm, p.room_norm,
       p.area_raw, p.rent_raw, p.partition_raw
FROM review_items r
JOIN tasks t ON t.id = r.task_id
LEFT JOIN properties p ON p.id = t.property_id
"""


def _row_id() -> str:
    return uuid.uuid4().hex[:16]


def create(task_id: str) -> str:
    """入队；已存在则原样返回（幂等）。"""
    now = utc_now()
    with transaction() as connection:
        existing = connection.execute(
            "SELECT id FROM review_items WHERE task_id = ?", (task_id,)
        ).fetchone()
        if existing:
            return existing["id"]
        new_id = _row_id()
        connection.execute(
            "INSERT INTO review_items (id, task_id, status, level, created_at, updated_at) "
            "VALUES (?, ?, 'pending', 'unknown', ?, ?)",
            (new_id, task_id, now, now),
        )
        return new_id


def get(task_id: str):
    with transaction() as connection:
        return connection.execute(
            "SELECT * FROM review_items WHERE task_id = ?", (task_id,)
        ).fetchone()


def update_precheck(task_id: str, level: str, payload: dict) -> None:
    create(task_id)
    with transaction() as connection:
        connection.execute(
            "UPDATE review_items SET level = ?, precheck_json = ?, updated_at = ? "
            "WHERE task_id = ?",
            (level, json_dumps(payload), utc_now(), task_id),
        )


def set_status(task_id: str, status: str, reviewer: str = "", note: str = "") -> None:
    create(task_id)
    now = utc_now()
    with transaction() as connection:
        connection.execute(
            "UPDATE review_items SET status = ?, reviewer = ?, note = ?, "
            "decided_at = ?, updated_at = ? WHERE task_id = ?",
            (
                status,
                reviewer,
                note[:500],
                now if status in ("approved", "rejected") else "",
                now,
                task_id,
            ),
        )


def list_items(status: str | None = None, limit: int = 50) -> list[dict]:
    sql = _LIST_SQL
    params: list = []
    if status:
        sql += " WHERE r.status = ?"
        params.append(status)
    sql += " ORDER BY r.created_at DESC LIMIT ?"
    params.append(limit)
    with transaction() as connection:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]


def detail(task_id: str) -> dict | None:
    with transaction() as connection:
        row = connection.execute(
            _LIST_SQL + " WHERE r.task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row else None


def missing_tasks(limit: int = 20) -> list[dict]:
    """已经出片但还没入队的任务。

    不在出片流程里硬插一步入队，是因为出片线程已经很忙，
    而且历史上已经出好的片子也需要补进来——用查询补齐比改流程更稳。
    """
    with transaction() as connection:
        rows = connection.execute(
            "SELECT t.id, t.task_no FROM tasks t "
            "LEFT JOIN review_items r ON r.task_id = t.id "
            "WHERE r.id IS NULL AND t.output_video_path != '' "
            "ORDER BY t.updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def counts() -> dict:
    with transaction() as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS n FROM review_items GROUP BY status"
        ).fetchall()
    result = {"pending": 0, "approved": 0, "rejected": 0, "published": 0}
    for row in rows:
        result[row["status"]] = row["n"]
    return result
