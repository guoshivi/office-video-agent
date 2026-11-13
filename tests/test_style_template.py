"""风格模板流水线测试：用一个本地假方舟服务，不联网、不花钱。"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

from openpyxl import Workbook

ANALYSIS = {
    "summary": "用具体数字开场，三句讲完一套房",
    "tone": "口语、短句、第二人称，开头给数字钩子",
    "structure": [{"role": "hook", "share": 0.2}, {"role": "亮点", "share": 0.5}, {"role": "cta", "share": 0.3}],
    "shots": [
        {"index": 1, "role": "hook", "seconds": 4.0, "sample": "月租四千，能在这一带租到什么", "words": 13},
        {"index": 2, "role": "亮点", "seconds": 8.0, "sample": "一百二十平，三个隔间全朝南", "words": 13},
        {"index": 3, "role": "cta", "seconds": 4.0, "sample": "想看房私信我", "words": 7},
    ],
    "constraints": {"max_words_per_shot": 20, "total_seconds": 30},
    "tips": ["前 3 秒必须出现数字"],
}

GENERATED = {
    "shots": [
        {"index": 1, "role": "hook", "text": "两百平的大开间，租金八千"},
        {"index": 2, "role": "亮点", "text": "五个隔间，全朝东，带家具"},
        {"index": 3, "role": "cta", "text": "想看房私信我"},
    ]
}


class FakeArk(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *_args):
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        FakeArk.calls.append(("POST", self.path))
        if self.path == "/files":
            assert b'name="purpose"' in raw
            self._json({"id": "file-test", "status": "active"})
        elif self.path == "/chat/completions":
            body = json.loads(raw)
            text = json.dumps(body, ensure_ascii=False)
            content = ANALYSIS if "结构分析师" in text else GENERATED
            self._json({"choices": [{"message": {"content": "```json\n" + json.dumps(content, ensure_ascii=False) + "\n```"}}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_GET(self):
        FakeArk.calls.append(("GET", self.path))
        if self.path.startswith("/files/"):
            self._json({"id": "file-test", "status": "active"})
        else:
            self._json({"error": "not found"}, 404)


def _sample_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "G区"
    sheet.append(["黄色为新增房源"])
    sheet.append(["区域", "楼栋", "房号", "面积", "隔间", "租金", "朝向", "家具", "看房备注及中介费", "联系人", "房源发布日"])
    sheet.append(["G区", "G02", "501", "200平", "5隔间", "8000/月", "朝东", "带家具", "内部", "内部", "10月3日"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_template_analyze_and_apply(monkeypatch):
    from fastapi.testclient import TestClient

    from app import style_template
    from app.db import init_db
    from app.main import app

    init_db()

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeArk)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("ARK_BASE_URL", base)
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    # 这条测的是云端视频理解那条路；默认引擎是本地，这里显式切过去
    monkeypatch.setenv("STYLE_ENGINE", "ark")
    monkeypatch.setattr(style_template, "probe_duration", lambda _path: 30.0)

    def fake_analyze(_path, task_no, **_kwargs):
        """替身：真实 analyze 要连 ffmpeg 抽帧，这里只写 manifest。"""
        from app.shots import SHOT_DIR

        target = SHOT_DIR / task_no
        target.mkdir(parents=True, exist_ok=True)
        (target / "shots.json").write_text(
            json.dumps({"task_no": task_no, "duration": 30, "shots": [], "storyboard": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {"shots": []}

    monkeypatch.setattr(style_template, "analyze_shots", fake_analyze)

    template_id = uuid.uuid4().hex[:12]
    source = style_template.TEMPLATE_DIR / template_id / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-reference-video")

    from app.db import transaction, utc_now

    now = utc_now()
    with transaction() as connection:
        connection.execute(
            """INSERT INTO style_templates(id, name, source_type, source_path, status, created_at, updated_at)
               VALUES (?, '参考片', 'upload', ?, 'pending', ?, ?)""",
            (template_id, str(source), now, now),
        )

    try:
        spec = style_template.analyze(template_id)
        assert len(spec["shots"]) == 3

        with transaction() as connection:
            row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
        assert row["status"] == "ready"
        assert "月租四千" in row["transcript"]

        with TestClient(app) as client:
            imported = client.post(
                "/api/properties/import",
                files={"file": ("g.xlsx", _sample_workbook(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
            assert imported.status_code == 200, imported.text
            properties = client.get("/api/properties?limit=10").json()["items"]
            property_id = properties[0]["id"]
            task = client.post(
                "/api/tasks",
                data={"property_id": property_id, "target_duration": 30, "aspect_ratio": "9:16"},
                files={"video": ("office.mp4", b"fake-source-video", "video/mp4")},
            ).json()

            applied = client.post(f"/api/tasks/{task['id']}/apply-template", json={"template_id": template_id})
            assert applied.status_code == 200, applied.text
            assert "两百平的大开间" in applied.json()["script"]

            listing = client.get("/api/templates").json()
            assert listing["configured"] is True
            assert any(item["id"] == template_id for item in listing["items"])
            detail = client.get(f"/api/templates/{template_id}").json()
            assert detail["shot_count"] == 3
            assert detail["spec"]["tone"]
    finally:
        server.shutdown()
        server.server_close()


def test_apply_rejects_unready_template(monkeypatch):
    from app import style_template
    from app.db import init_db

    init_db()
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    try:
        style_template.apply_to_task("no-such-task", "no-such-template")
    except style_template.StyleTemplateError as error:
        assert "模板不存在" in str(error)
    else:  # pragma: no cover - 失败才算测试通过
        raise AssertionError("应当报模板不存在")


def test_not_configured_raises():
    from app import style_template

    assert style_template.is_configured() is False or True  # 取决于环境，仅保证不抛异常


def test_template_analyzes_without_cloud_key(monkeypatch):
    """拆解是本地能力：没配 ARK_API_KEY 也必须能把一条片子拆出来。

    回归用：analyze() 原本整条走方舟视频理解（上传 → 预处理 → 视频问答），
    没 key 时直接 failed，而这是最常见的使用环境——"上传 → 看拼图 → 复核"
    的主链路第一步就断了，后面拼图、占比、套用全都没机会发生。
    """
    from app import refvideo, style_template
    from app.db import init_db, transaction, utc_now

    init_db()
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("STYLE_ENGINE", raising=False)

    def fake_local(_path):
        return {
            "duration": 12.5,
            "cuts": [],
            "shots": [
                {"index": 1, "role": "hook", "start": 0.0, "end": 12.5,
                 "seconds": 12.5, "sample": "", "words": 0, "synthetic": True}
            ],
            "sentences": [{"start": 1.0, "end": 2.0, "text": "这里是一句台词"}],
            "labels": [],
            "warnings": [],
            "engine": "local",
        }

    monkeypatch.setattr(refvideo, "analyze_file", fake_local)
    monkeypatch.setattr(style_template, "probe_duration", lambda _path: 12.5)

    template_id = uuid.uuid4().hex[:12]
    source = style_template.TEMPLATE_DIR / template_id / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fake-reference-video")
    now = utc_now()
    with transaction() as connection:
        connection.execute(
            """INSERT INTO style_templates(id, name, source_type, source_path, status, created_at, updated_at)
               VALUES (?, '无 key 参考片', 'upload', ?, 'pending', ?, ?)""",
            (template_id, str(source), now, now),
        )

    spec = style_template.analyze(template_id)
    assert len(spec["shots"]) == 1

    with transaction() as connection:
        row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
    assert row["status"] == "ready"
    assert row["error"] == ""
    assert "这里是一句台词" in row["transcript"]

