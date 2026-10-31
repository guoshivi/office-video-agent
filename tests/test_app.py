import json
import os
import tempfile
from io import BytesIO
from pathlib import Path

os.environ["OVA_DATA_DIR"] = tempfile.mkdtemp(prefix="office-video-agent-test-")

from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from app.main import app


def sample_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "A区"
    sheet.append(["黄色为新增房源，红色字体为特别标注"])
    sheet.append(["区域", "楼栋", "房号", "面积", "隔间", "租金", "朝向", "家具", "看房备注及中介费", "联系人", "房源发布日"])
    sheet.append(["A区", "A01", "101", "100平", "2隔", "40", "朝北", "带家具", "内部备注", "内部联系人", "10月1日"])
    sheet.append([None, None, "102", "120平", "3隔", "1400/月", "朝南", "带家具", "钥匙在管理处", "内部联系人", "10月2日"])
    sheet.append([None, None, "102", "120平", "3隔", "1400/月", "朝南", "带家具", "钥匙在管理处", "内部联系人", "10月2日"])
    for cell in sheet[3]:
        cell.fill = PatternFill("solid", fgColor="FFFF00")
    sheet["A4"].font = Font(color="FF0000")
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_full_local_workflow():
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "local"

        excel = sample_workbook()
        response = client.post(
            "/api/properties/import",
            files={"file": ("listings.xlsx", excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["total_rows"] == 3
        assert result["inserted_rows"] == 3
        assert result["review_rows"] == 2

        duplicate_import = client.post(
            "/api/properties/import",
            files={"file": ("listings.xlsx", excel, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert duplicate_import.json()["skipped_rows"] == 3

        properties = client.get("/api/properties?limit=100").json()
        assert properties["total"] == 3
        first = properties["items"][0]
        assert "contact_raw" not in first
        assert "viewing_commission_raw" not in first
        marked = next(item for item in properties["items"] if item["room"] == "101")
        assert marked["is_new_marked"] is True
        special = next(item for item in properties["items"] if item["room"] == "102" and item["is_special_marked"])
        assert special["is_special_marked"] is True
        assert special["region"] == "A区"
        assert special["building"] == "A01"

        task_response = client.post(
            "/api/tasks",
            data={"property_id": special["id"], "target_duration": 45, "aspect_ratio": "9:16"},
            files={"video": ("tour.mp4", b"fake-video-for-api-test", "video/mp4")},
        )
        assert task_response.status_code == 200, task_response.text
        task = task_response.json()
        assert task["status"] == "script_ready"
        assert "内部联系人" not in task["script"]
        assert "钥匙" not in task["script"]
        assert task["warnings"]

        script_update = client.put(
            f"/api/tasks/{task['id']}/script",
            json={"script": "这是一条经过人工确认的口播。"},
        )
        assert script_update.status_code == 200

        export = client.post(f"/api/tasks/{task['id']}/export")
        assert export.status_code == 200, export.text
        manifest_path = Path(export.json()["manifest_path"])
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["render"]["aspect_ratio"] == "9:16"
        assert "contact_raw" not in manifest["property"]
        assert "not a rendered video" in manifest["note"]

        dashboard = client.get("/api/dashboard").json()
        assert dashboard["property_count"] == 3
        assert dashboard["task_count"] == 1
