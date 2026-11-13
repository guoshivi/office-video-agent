"""手工新增房源：Excel 之外的入口，字段口径必须和导入一致。

重点验三件事：
1. 存进去就能被列表搜到、能直接拿去建任务；
2. 同房号不静默覆盖——先 409 报出「那是哪一套」，用户确认才改；
3. 覆盖一条 Excel 导入来的房，不能把它变成「下次重导就多一条副本」。
"""

import uuid
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.db import transaction
from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def unique_room() -> str:
    return f"P{uuid.uuid4().hex[:6].upper()}"


def draft(**overrides) -> dict:
    data = {
        "region": "测试区",
        "building": "T1",
        "room": unique_room(),
        "area": "230平",
        "partition": "3隔",
        "rent": "40",
        "orientation": "南",
        "furnishing_layout": "精装 3+1",
        "viewing_commission": "随时可看，佣金半个月",
        "contact": "小王",
        "publish_date": "2026-11-13",
    }
    data.update(overrides)
    return data


def workbook_bytes(room: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "B区"
    sheet.append(["黄色为新增房源"])
    sheet.append(["区域", "楼栋", "房号", "面积", "隔间", "租金", "朝向", "家具", "看房备注及中介费", "联系人", "房源发布日"])
    sheet.append(["B区", "B1", room, "100平", "2隔", "30", "南", "带家具", "内部备注", "内部联系人", ""])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def import_sheet(client, room: str) -> dict:
    response = client.post(
        "/api/properties/import",
        files={"file": ("listings.xlsx", workbook_bytes(room), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_new_property_is_searchable_and_ready(client):
    data = draft()
    response = client.post("/api/properties", json=data)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True and body["updated"] is False

    prop = body["property"]
    assert prop["room"] == data["room"]
    assert prop["area_sqm"] == 230
    assert prop["status"] == "active"
    assert prop["source_sheet"] == "手动录入"
    assert prop["source_row"] == 0
    assert prop["is_new_marked"] is True

    # 列表按房号能搜到，且能拿到 id 去建任务
    found = client.get("/api/properties", params={"search": data["room"]}).json()
    assert [item["id"] for item in found["items"]] == [prop["id"]]


def test_room_and_area_are_required(client):
    assert client.post("/api/properties", json={"area": "120"}).status_code == 400
    assert client.post("/api/properties", json={"room": "A101"}).status_code == 400


def test_missing_region_goes_to_review_instead_of_failing(client):
    # 现场只记得楼栋房号也要能存，只是会被标成待确认
    response = client.post("/api/properties", json={"room": unique_room(), "area": "80"})
    assert response.status_code == 201
    assert response.json()["property"]["status"] == "needs_review"


def test_duplicate_room_asks_before_overwriting(client):
    data = draft()
    first = client.post("/api/properties", json=data).json()["property"]

    again = client.post("/api/properties", json=data)
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["existing"]["id"] == first["id"]
    assert data["room"] in detail["existing"]["label"]

    # 空字段不覆盖：这次只改租金，楼栋保持原值
    override = client.post(
        "/api/properties",
        json={"room": data["room"], "area": data["area"], "rent": "45含物业", "overwrite_id": first["id"]},
    )
    assert override.status_code == 201, override.text
    body = override.json()
    assert body["created"] is False and body["updated"] is True
    assert body["property"]["id"] == first["id"]
    assert body["property"]["rent"] == "45含物业"
    assert body["property"]["building"] == "T1"

    # 覆盖不是复制：库里仍然只有这一条
    same = client.get("/api/properties", params={"search": data["room"]}).json()["items"]
    assert len(same) == 1


def test_overwrite_keeps_import_fingerprint(client):
    """手工改了 Excel 来的房源，下次重导同一份表不能再插一条副本。"""
    room = unique_room()
    assert import_sheet(client, room)["inserted_rows"] == 1
    with transaction() as connection:
        row = connection.execute("SELECT * FROM properties WHERE room_norm=?", (room,)).fetchone()
    assert row is not None and row["source_row"] == 3

    response = client.post(
        "/api/properties",
        json={
            "region": "B区", "building": "B1", "room": room,
            "area": "100平", "rent": "35", "overwrite_id": row["id"],
        },
    )
    assert response.status_code == 201, response.text
    updated = response.json()["property"]
    assert updated["source_sheet"] == "B区"
    assert updated["source_row"] == 3
    assert updated["rent"] == "35"

    again = import_sheet(client, room)
    assert again["skipped_rows"] == 1
    assert again["inserted_rows"] == 0
    with transaction() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM properties WHERE match_key=?", (row["match_key"],)
        ).fetchone()[0]
    assert count == 1
