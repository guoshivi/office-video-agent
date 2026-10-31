import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import date, datetime
from io import BytesIO

from openpyxl import load_workbook
from openpyxl.styles.colors import COLOR_INDEX

from .db import json_dumps, transaction, utc_now

FIELD_ALIASES = {
    "区域": "region_raw",
    "楼栋": "building_raw",
    "房号": "room_raw",
    "面积": "area_raw",
    "隔间": "partition_raw",
    "租金": "rent_raw",
    "朝向": "orientation_raw",
    "看房备注及中介费": "viewing_commission_raw",
    "联系人": "contact_raw",
}

PROPERTY_COLUMNS = [
    "id", "match_key", "region_raw", "building_raw", "room_raw",
    "region_norm", "building_norm", "room_norm", "area_raw", "area_sqm",
    "partition_raw", "rent_raw", "orientation_raw", "furnishing_layout_raw",
    "viewing_commission_raw", "contact_raw", "publish_date_raw", "source_file",
    "source_sheet", "source_row", "source_fingerprint", "import_batch",
    "raw_row_json", "fill_color", "font_color", "is_new_marked",
    "is_special_marked", "status", "created_at", "updated_at",
]

UPDATE_COLUMNS = [
    column for column in PROPERTY_COLUMNS
    if column not in {"id", "created_at"}
]

YELLOW_RGB = {"FFFF00", "FFF200", "FFD966", "FFF2CC", "FFFF99", "FFFFFF00"}
RED_RGB = {"FF0000", "C00000", "E06666", "FFFF0000"}


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def key_part(value) -> str:
    return re.sub(r"\s+", "", text(value)).upper()


def parse_area(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:平|㎡|m²)?", text(value), re.I)
    return float(match.group(1)) if match else None


def color_rgb(color) -> str:
    if color is None:
        return ""
    try:
        if color.type == "rgb" and color.rgb:
            return str(color.rgb).upper()
        if color.type == "indexed" and color.indexed is not None:
            index = int(color.indexed)
            if 0 <= index < len(COLOR_INDEX):
                return COLOR_INDEX[index].upper()
        if color.type == "theme":
            return f"THEME:{color.theme}"
    except (TypeError, ValueError, IndexError):
        return ""
    return ""


def row_colors(row) -> tuple[str, str, int, int]:
    fills, fonts = set(), set()
    yellow, red = False, False
    for cell in row:
        fill = color_rgb(cell.fill.fgColor) if cell.fill and cell.fill.fill_type else ""
        font = color_rgb(cell.font.color) if cell.font else ""
        if fill:
            fills.add(fill)
            yellow = yellow or fill in YELLOW_RGB or fill.endswith("FFFF00")
        if font:
            fonts.add(font)
            red = red or font in RED_RGB or font.endswith("FF0000")
    return ";".join(sorted(fills)), ";".join(sorted(fonts)), int(yellow), int(red)


def header_mapping(worksheet) -> dict[str, int]:
    mapping = {}
    for column in range(1, 12):
        header = text(worksheet.cell(2, column).value)
        if header in FIELD_ALIASES:
            mapping[FIELD_ALIASES[header]] = column
        elif header.startswith("家具"):
            mapping["furnishing_layout_raw"] = column
        elif header.startswith("房源发布"):
            mapping["publish_date_raw"] = column
    required = {"region_raw", "room_raw", "area_raw", "rent_raw"}
    if not required.issubset(mapping):
        raise ValueError(f"工作表 {worksheet.title} 的第 2 行不是受支持的房源表头")
    return mapping


def insert_property(connection, prop: dict) -> None:
    columns = ",".join(PROPERTY_COLUMNS)
    placeholders = ",".join("?" for _ in PROPERTY_COLUMNS)
    connection.execute(
        f"INSERT INTO properties ({columns}) VALUES ({placeholders})",
        [prop.get(column) for column in PROPERTY_COLUMNS],
    )


def update_property(connection, prop: dict) -> None:
    assignments = ",".join(f"{column}=?" for column in UPDATE_COLUMNS)
    values = [prop.get(column) for column in UPDATE_COLUMNS]
    values.append(prop["id"])
    connection.execute(f"UPDATE properties SET {assignments} WHERE id=?", values)


def add_version(connection, prop: dict) -> None:
    connection.execute(
        "INSERT INTO property_versions(property_id, import_batch, snapshot_json, created_at) VALUES (?, ?, ?, ?)",
        (prop["id"], prop["import_batch"], json_dumps(prop), utc_now()),
    )


def import_excel(content: bytes, filename: str) -> dict:
    if not content:
        raise ValueError("Excel 文件为空")
    source_sha256 = hashlib.sha256(content).hexdigest()
    batch_id = str(uuid.uuid4())
    workbook = load_workbook(BytesIO(content), data_only=True, read_only=False)
    records = []

    for worksheet in workbook.worksheets:
        mapping = header_mapping(worksheet)
        # Some sheets leave the region column entirely empty. Use the sheet name
        # only as the initial context; any explicit in-sheet region takes over.
        last_region, last_building = worksheet.title, ""
        for row_index in range(3, worksheet.max_row + 1):
            row = worksheet[row_index]
            raw = {field: text(worksheet.cell(row_index, column).value) for field, column in mapping.items()}
            for field in FIELD_ALIASES.values():
                raw.setdefault(field, "")
            raw.setdefault("furnishing_layout_raw", "")
            raw.setdefault("publish_date_raw", "")
            if not any(raw.values()):
                continue

            if raw["region_raw"]:
                if key_part(raw["region_raw"]) != key_part(last_region):
                    last_building = ""
                last_region = raw["region_raw"]
            if raw["building_raw"]:
                last_building = raw["building_raw"]
            region_norm = raw["region_raw"] or last_region
            building_norm = raw["building_raw"] or last_building
            room_norm = raw["room_raw"]
            parts = [key_part(region_norm), key_part(building_norm), key_part(room_norm)]
            match_key = "|".join(parts) if all(parts) else ""
            fill_color, font_color, is_new, is_special = row_colors(row[:11])
            fingerprint = hashlib.sha256(
                f"{source_sha256}:{worksheet.title}:{row_index}".encode("utf-8")
            ).hexdigest()
            now = utc_now()
            prop = {
                "id": str(uuid.uuid4()),
                "match_key": match_key,
                **raw,
                "region_norm": region_norm,
                "building_norm": building_norm,
                "room_norm": room_norm,
                "area_sqm": parse_area(raw["area_raw"]),
                "source_file": filename,
                "source_sheet": worksheet.title,
                "source_row": row_index,
                "source_fingerprint": fingerprint,
                "import_batch": batch_id,
                "raw_row_json": json_dumps(raw),
                "fill_color": fill_color,
                "font_color": font_color,
                "is_new_marked": is_new,
                "is_special_marked": is_special,
                "status": "active" if match_key else "needs_review",
                "created_at": now,
                "updated_at": now,
            }
            records.append(prop)

    duplicate_keys = {
        key for key, count in Counter(p["match_key"] for p in records if p["match_key"]).items()
        if count > 1
    }
    stats = {"inserted_rows": 0, "updated_rows": 0, "skipped_rows": 0, "review_rows": 0}

    with transaction() as connection:
        for prop in records:
            exists = connection.execute(
                "SELECT id FROM properties WHERE source_fingerprint=?",
                (prop["source_fingerprint"],),
            ).fetchone()
            if exists:
                stats["skipped_rows"] += 1
                continue

            key = prop["match_key"]
            existing = connection.execute(
                "SELECT id, status FROM properties WHERE match_key=? AND match_key<>''",
                (key,),
            ).fetchall() if key else []

            if key in duplicate_keys or len(existing) > 1 or not key:
                prop["status"] = "needs_review"
                insert_property(connection, prop)
                add_version(connection, prop)
                stats["inserted_rows"] += 1
            elif len(existing) == 1:
                prop["id"] = existing[0]["id"]
                prop["status"] = existing[0]["status"]
                update_property(connection, prop)
                add_version(connection, prop)
                stats["updated_rows"] += 1
            else:
                insert_property(connection, prop)
                add_version(connection, prop)
                stats["inserted_rows"] += 1

            if prop["status"] == "needs_review":
                stats["review_rows"] += 1

        connection.execute(
            """INSERT INTO import_batches(
                id, filename, source_sha256, total_rows, inserted_rows,
                updated_rows, skipped_rows, review_rows, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_id, filename, source_sha256, len(records),
                stats["inserted_rows"], stats["updated_rows"],
                stats["skipped_rows"], stats["review_rows"], utc_now(),
            ),
        )

    return {"batch_id": batch_id, "filename": filename, "total_rows": len(records), **stats}
