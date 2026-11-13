"""手工新增房源：Excel 里没有的房，直接在前端补一条。

Excel 是批量入口，但真正跑业务的人每天都在遇到「表里没有、刚谈下来」的房。
让人为一条房回家改 Excel 再整本重导，代价不是麻烦，是风险：
整本重导会把别人这几天改过的行一起覆盖回去。所以这里只写一条。

字段口径和导入完全一致，不另立一套：
- match_key（区域|楼栋|房号）决定这条房能不能被搜到、能不能算「同一套」；
- 面积必须能解析成数字，否则口播算不出工位和人均；
- 租金存原文（40、35+5、40含物业），念的时候由文案层统一口径。
"""

from __future__ import annotations

import hashlib
import uuid

from .db import init_db, json_dumps, transaction, utc_now
from .excel_import import add_version, insert_property, key_part, parse_area, text

MANUAL_SOURCE = "手动录入"
MANUAL_BATCH = "manual"

# 前端字段名 → 数据库原始列名。只收这些，别的一律不认，
# 免得有人从接口塞 status / import_batch 这种内部字段进来。
FIELDS = {
    "region": "region_raw",
    "building": "building_raw",
    "room": "room_raw",
    "area": "area_raw",
    "partition": "partition_raw",
    "rent": "rent_raw",
    "orientation": "orientation_raw",
    "furnishing_layout": "furnishing_layout_raw",
    "viewing_commission": "viewing_commission_raw",
    "contact": "contact_raw",
    "publish_date": "publish_date_raw",
}


class PropertyEntryError(ValueError):
    """字段不合格，或者这套房已经有记录了。"""


class PropertyConflict(PropertyEntryError):
    """同房号已经有记录。带回去让前端问一句「要不要覆盖」。"""

    def __init__(self, message: str, existing: dict):
        super().__init__(message)
        self.existing = existing


def _match_key(region: str, building: str, room: str) -> str:
    parts = [key_part(region), key_part(building), key_part(room)]
    return "|".join(parts) if all(parts) else ""


def _label(region: str, building: str, room: str) -> str:
    return " · ".join(part for part in (region, building, room) if part) or room


def _brief(row) -> dict:
    return {
        "id": row["id"],
        "label": _label(row["region_norm"], row["building_norm"], row["room_norm"]),
        "area": row["area_raw"],
        "rent": row["rent_raw"],
        "status": row["status"],
        "source": row["source_sheet"] or row["source_file"],
        "updated_at": row["updated_at"],
    }


def _find_by_key(connection, match_key: str, exclude_id: str = "") -> list:
    if not match_key:
        return []
    rows = connection.execute(
        "SELECT * FROM properties WHERE match_key=? ORDER BY created_at", (match_key,)
    ).fetchall()
    return [row for row in rows if row["id"] != exclude_id]


def create_property(data: dict, overwrite_id: str = ""):
    """新增一条房源；overwrite_id 给定时，用这次填的值覆盖那一条旧记录。

    覆盖只覆盖「这次填了内容的字段」——表单里空着的项保持原值。
    不这么做的话，用户只想改个租金，回来一看区域楼栋被清空、房源
    直接掉进待确认，那种惊吓没人受得了。

    覆盖按 id 而不是按房号：用户可能就是来纠正填错的房号的，
    这时候「按房号找要覆盖谁」永远找不到——找的是改完以后的新房号。
    """
    init_db()
    incoming = {field: text(data.get(field, "")) for field in FIELDS}

    if not incoming["room"]:
        raise PropertyEntryError("房号必须填——不然列表里分不清是哪一间")
    if not incoming["area"]:
        raise PropertyEntryError("面积必须填——口播要按面积算工位和人均")

    match_key = _match_key(incoming["region"], incoming["building"], incoming["room"])
    existing = None
    with transaction() as connection:
        if overwrite_id:
            existing = connection.execute(
                "SELECT * FROM properties WHERE id=?", (overwrite_id,)
            ).fetchone()
            if existing is None:
                raise PropertyEntryError("要覆盖的房源已经不在了，刷新列表重新来")
            clash = _find_by_key(connection, match_key, exclude_id=existing["id"])
        else:
            same_key = _find_by_key(connection, match_key)
            if len(same_key) > 1:
                raise PropertyEntryError(
                    f"库里已经有 {len(same_key)} 条"
                    f"「{_label(incoming['region'], incoming['building'], incoming['room'])}」，"
                    "请先在 Excel 里合并重复行，再新增"
                )
            existing = same_key[0] if same_key else None
            clash = []
    if existing is not None and not overwrite_id:
        raise PropertyConflict("这套房已经在库里了", _brief(existing))
    if clash:
        raise PropertyEntryError(
            f"改成「{_label(incoming['region'], incoming['building'], incoming['room'])}」会和库里另一条重复"
        )

    now = utc_now()
    if existing is not None:
        values = {
            field: incoming[field] or (existing[column] or "")
            for field, column in FIELDS.items()
        }
        match_key = _match_key(values["region"], values["building"], values["room"])
        if match_key and match_key != existing["match_key"]:
            with transaction() as connection:
                clash = _find_by_key(connection, match_key, exclude_id=existing["id"])
            if clash:
                raise PropertyEntryError(
                    f"改成「{_label(values['region'], values['building'], values['room'])}」会和库里另一条重复"
                )
        prop = {
            "id": existing["id"],
            **{column: values[field] for field, column in FIELDS.items()},
            "match_key": match_key,
            "region_norm": values["region"],
            "building_norm": values["building"],
            "room_norm": values["room"],
            "area_sqm": parse_area(values["area"]),
            "status": "active" if match_key else "needs_review",
            # 来源和指纹保持原样：这条房还是从那个 Excel 行来的，
            # 改掉指纹会让下次重导同一份 Excel 时凭空多出一条副本。
            "source_file": existing["source_file"] or MANUAL_SOURCE,
            "source_sheet": existing["source_sheet"] or MANUAL_SOURCE,
            "source_row": existing["source_row"],
            "source_fingerprint": existing["source_fingerprint"],
            "import_batch": existing["import_batch"] or MANUAL_BATCH,
            "raw_row_json": json_dumps(
                {column: values[field] for field, column in FIELDS.items()}
            ),
            "fill_color": existing["fill_color"],
            "font_color": existing["font_color"],
            "is_new_marked": existing["is_new_marked"],
            "is_special_marked": existing["is_special_marked"],
            "created_at": existing["created_at"],
            "updated_at": now,
        }
        columns = ",".join(f"{column}=?" for column in prop if column != "id")
        params = [prop[column] for column in prop if column != "id"] + [prop["id"]]
        with transaction() as connection:
            connection.execute(f"UPDATE properties SET {columns} WHERE id=?", params)
            add_version(connection, prop)
            row = connection.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
        return {"created": False, "updated": True, "property": dict(row)}

    batch_id = f"{MANUAL_BATCH}-{uuid.uuid4().hex[:8]}"
    prop = {
        "id": str(uuid.uuid4()),
        **{column: incoming[field] for field, column in FIELDS.items()},
        "match_key": match_key,
        "region_norm": incoming["region"],
        "building_norm": incoming["building"],
        "room_norm": incoming["room"],
        "area_sqm": parse_area(incoming["area"]),
        "source_file": MANUAL_SOURCE,
        "source_sheet": MANUAL_SOURCE,
        "source_row": 0,
        # 手动录入没有来源行，指纹按批次 + uuid 生成，别和 Excel 的撞
        "source_fingerprint": hashlib.sha256(
            f"manual:{batch_id}:{uuid.uuid4()}".encode("utf-8")
        ).hexdigest(),
        "import_batch": batch_id,
        "raw_row_json": json_dumps(
            {column: incoming[field] for field, column in FIELDS.items()}
        ),
        "fill_color": "",
        "font_color": "",
        "is_new_marked": 1,
        "is_special_marked": 0,
        "status": "active" if match_key else "needs_review",
        "created_at": now,
        "updated_at": now,
    }
    with transaction() as connection:
        insert_property(connection, prop)
        add_version(connection, prop)
        row = connection.execute("SELECT * FROM properties WHERE id=?", (prop["id"],)).fetchone()
    return {"created": True, "updated": False, "property": dict(row)}
