import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

os.environ.setdefault("OVA_DATA_DIR", tempfile.mkdtemp(prefix="office-video-agent-test-"))

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


def test_full_local_workflow(monkeypatch):
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
            data={"property_id": special["id"], "target_duration": 45, "aspect_ratio": "9:16", "audio_mode": "voice"},
            files={"video": ("tour.mp4", b"fake-video-for-api-test", "video/mp4")},
        )
        assert task_response.status_code == 200, task_response.text
        task = task_response.json()
        assert task["status"] == "script_ready"
        # 这条任务自己选了"不要原声"，不能被设置里的默认值盖掉
        assert task["audio_mode"] == "voice"
        assert "内部联系人" not in task["script"]
        assert "钥匙" not in task["script"]
        assert task["warnings"]

        script_update = client.put(
            f"/api/tasks/{task['id']}/script",
            json={"script": "这是一条经过人工确认的口播。"},
        )
        assert script_update.status_code == 200

        audio_update = client.put(f"/api/tasks/{task['id']}/audio", json={"audio_mode": "mix"})
        assert audio_update.status_code == 200
        assert audio_update.json()["audio_mode"] == "mix"
        bad_audio = client.put(f"/api/tasks/{task['id']}/audio", json={"audio_mode": "loud"})
        assert bad_audio.status_code == 400

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

        review_property = client.get("/api/properties?status=needs_review").json()["items"][0]
        confirmed = client.post(f"/api/properties/{review_property['id']}/confirm")
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "active"

        from app import semantic

        async def fake_embed(texts):
            return [[1.0, 0.0, 0.0] if "A区" in text else [0.0, 1.0, 0.0] for text in texts]

        monkeypatch.setenv("SEMANTIC_PROVIDER", "http")
        monkeypatch.setenv("SEMANTIC_BASE_URL", "http://127.0.0.1:9999/v1")
        monkeypatch.setenv("SEMANTIC_MODEL", "local-test-embedding")
        monkeypatch.setattr(semantic, "embed_texts", fake_embed)
        indexed = client.post("/api/semantic/reindex")
        assert indexed.status_code == 200, indexed.text
        assert indexed.json()["indexed_count"] == 3
        semantic_hits = client.get("/api/semantic/search?q=安静的小办公室&limit=5")
        assert semantic_hits.status_code == 200, semantic_hits.text
        assert semantic_hits.json()["total"] == 3
        assert "contact_raw" not in semantic_hits.json()["items"][0]


def test_workbench_render_flow():
    calls = []
    # 工作台只接受授权根目录内的路径，测试里模拟一块可写的“NAS 根”
    material_root = Path(tempfile.mkdtemp(prefix="ova-nas-root-"))

    class MockWorkbench(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            calls.append(("GET", self.path, self.headers.get("Origin")))
            if self.path == "/api/worker":
                self._json({"alive": True, "running_job": None, "disk": {"ok": True}})
            elif self.path == "/api/roots":
                self._json([{"id": 0, "name": "nas", "path": str(material_root)}])
            elif self.path.startswith("/api/products/p1/index"):
                self._json({"ready": True, "index": {"count": 1}, "tasks": []})
            elif self.path.startswith("/api/jobs"):
                self._json([{"id": "j1", "status": "done", "stage": "export", "progress": 100, "output": "mock.mp4"}])
            elif self.path.startswith("/api/results/j1"):
                body = b"rendered-video"
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            calls.append(("POST", self.path, self.headers.get("Origin")))
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path == "/api/products":
                self._json({"id": "p1", "name": "test"}, 201)
            elif self.path.endswith("/jobs"):
                self._json({"id": "j1", "status": "queued"}, 202)
            else:
                self._json({}, 200)

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockWorkbench)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with TestClient(app) as client:
            properties = client.get("/api/properties?limit=10").json()["items"]
            task = client.post(
                "/api/tasks",
                data={"property_id": properties[0]["id"], "target_duration": 30, "aspect_ratio": "16:9"},
                files={"video": ("office.mp4", b"fake-source-video", "video/mp4")},
            ).json()
            settings = client.get("/api/settings").json()
            settings["default_duration"] = int(settings["default_duration"])
            # 这条用例验的是「接远程工作台」这条路，所以必须显式选工作台：
            # 默认引擎已经改成本机出片（发给别人的电脑上没有工作台）
            settings.update({
                "workbench_url": base_url,
                "workbench_output_root": str(Path(tempfile.gettempdir()) / "ova-workbench-test"),
                "render_engine": "workbench",
            })
            saved = client.put("/api/settings", json={
                key: settings[key] for key in (
                    "public_contact", "default_duration", "default_aspect_ratio",
                    "default_voice", "workbench_path", "workbench_url", "workbench_output_root",
                    "render_engine",
                )
            })
            assert saved.status_code == 200, saved.text
            workbench_state = client.get("/api/workbench/status").json()
            assert workbench_state["online"] is True
            response = client.post(f"/api/tasks/{task['id']}/render")
            assert response.status_code == 202, response.text
            finished = client.get(f"/api/tasks/{task['id']}").json()
            assert finished["status"] == "completed", finished
            assert finished["progress"] == 100
            assert Path(finished["output_video_path"]).read_bytes() == b"rendered-video"
            # 素材必须被镜像到授权根目录内，否则真实工作台会 400
            staged = list((material_root / "ova-agent").rglob("*.mp4"))
            assert staged, "source video must be staged inside the authorized root"
            assert client.get(f"/api/tasks/{task['id']}/output").status_code == 200
            assert any(call[0] == "POST" and call[1] == "/api/products" and call[2] == base_url for call in calls)
    finally:
        server.shutdown()
        server.server_close()
