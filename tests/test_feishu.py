import json
import os
import tempfile
import time
from io import BytesIO
from pathlib import Path

os.environ.setdefault("OVA_DATA_DIR", tempfile.mkdtemp(prefix="office-video-agent-test-"))

from openpyxl import Workbook

from app.db import init_db, transaction
from app.excel_import import import_excel
from app.feishu.client import FeishuClient, FeishuConfig, FeishuError
from app.feishu.handlers import FeishuAgent, safe_filename
from app.feishu.intents import parse_command, resolve_property


def sample_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "A区"
    sheet.append(["黄色为新增房源，红色字体为特别标注"])
    sheet.append(["区域", "楼栋", "房号", "面积", "隔间", "租金", "朝向", "家具", "看房备注及中介费", "联系人", "房源发布日"])
    sheet.append(["A区", "A01", "101", "100平", "2隔", "40", "朝北", "带家具", "内部备注", "内部联系人", "10月1日"])
    sheet.append([None, None, "102", "120平", "3隔", "1400/月", "朝南", "带家具", "钥匙在管理处", "内部联系人", "10月2日"])
    sheet.append([None, None, "102", "120平", "3隔", "1400/月", "朝南", "带家具", "钥匙在管理处", "内部联系人", "10月2日"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


class FakeClient:
    """只实现 FeishuAgent 用到的方法，避免测试触碰网络。"""

    def __init__(self):
        self.config = FeishuConfig()
        self.sent: list[tuple[str, str]] = []
        self.payloads: dict[str, bytes] = {}

    def send_text(self, chat_id: str, text: str):
        self.sent.append((chat_id, text))
        return {"code": 0}

    def download_resource(self, message_id: str, file_key: str, destination: Path, kind: str = "file") -> int:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self.payloads.get(file_key, b"\x00" * 2048)
        destination.write_bytes(data)
        return len(data)

    def upload_file(self, path, file_type="mp4", duration_ms=0):
        # 模拟上传失败，验证降级到文本路径提示的分支
        raise FeishuError("mock upload failure")

    def bitable_search(self, *args, **kwargs):
        return []


def prepare() -> FakeClient:
    init_db()
    with transaction() as connection:
        connection.execute("DELETE FROM tasks")
        connection.execute("DELETE FROM feishu_sessions")
        connection.execute("DELETE FROM feishu_events")
        connection.execute("DELETE FROM property_versions")
        connection.execute("DELETE FROM property_embeddings")
        connection.execute("DELETE FROM properties")
    import_excel(sample_workbook(), "test-listings.xlsx")
    return FakeClient()


def text_event(message_id: str, body: str, chat_id: str = "oc_test") -> dict:
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "message_type": "text",
        "content": json.dumps({"text": body}, ensure_ascii=False),
    }


def media_event(message_id: str, file_key: str = "file_key_1", chat_id: str = "oc_test") -> dict:
    return {
        "message_id": message_id,
        "chat_id": chat_id,
        "message_type": "media",
        "content": json.dumps(
            {"file_key": file_key, "file_name": "办公室实拍.mp4", "duration": 18000}
        ),
    }


# ------------------------------------------------------------------ 意图解析
def test_parse_command_variants():
    assert parse_command("出片 V20251101-ABCDE").action == "render"
    assert parse_command("出片 V20251101-ABCDE").task_no == "V20251101-ABCDE"
    assert parse_command("出片=V20251101-ABCDE").task_no == "V20251101-ABCDE"
    assert parse_command("帮助").action == "help"
    assert parse_command("取消").action == "cancel"
    script = parse_command("改口播 V20251101-ABCDE 这套办公室朝南采光很好")
    assert script.action == "script"
    assert script.task_no == "V20251101-ABCDE"
    assert script.payload == "这套办公室朝南采光很好"
    assert parse_command("A区 A01 101") is None


def test_resolve_property_fuzzy_and_ambiguous():
    prepare()
    exact = resolve_property("A区 A01 101")
    assert exact.kind == "matched"
    assert exact.property["room_norm"] == "101"

    # 少写区域也能定位
    loose = resolve_property("A01 101")
    assert loose.kind == "matched"

    # 只有房号、且原表里是重复房源 → 交给人工选
    ambiguous = resolve_property("102")
    assert ambiguous.kind == "ambiguous"
    assert len(ambiguous.candidates) == 2

    assert resolve_property("B区 99号楼 999").kind == "none"


def test_safe_filename():
    assert safe_filename("办公室 实拍.mp4").endswith(".mp4")
    assert safe_filename("../../etc/passwd").endswith(".mp4")
    assert "/" not in safe_filename("a/b/c.mov")


# ------------------------------------------------------------------ 主流程
def test_video_then_property_creates_task():
    client = prepare()
    agent = FeishuAgent(client)

    action = agent.handle_event(media_event("om_video_1"))
    assert action.startswith("video_saved"), action
    assert "视频已收到" in client.sent[-1][1]
    assert "下一步" in client.sent[-1][1]

    action = agent.handle_event(text_event("om_text_1", "A区 A01 101"))
    assert action.startswith("task_created"), action

    reply = client.sent[-1][1]
    assert "口播草稿" in reply
    assert "出片 V" in reply
    # 内部信息绝不能出现在飞书回执里
    assert "内部联系人" not in reply
    assert "钥匙" not in reply

    with transaction() as connection:
        row = connection.execute("SELECT * FROM tasks").fetchone()
        session = connection.execute("SELECT * FROM feishu_sessions").fetchall()
    assert row["source"] == "feishu"
    assert row["notify_chat_id"] == "oc_test"
    assert row["status"] == "script_ready"
    assert Path(row["source_video_path"]).is_file()
    assert session == []  # 绑定成功后会话被清掉，避免串单


def test_video_kept_when_property_not_found():
    client = prepare()
    agent = FeishuAgent(client)
    agent.handle_event(media_event("om_video_2"))
    action = agent.handle_event(text_event("om_text_2", "不存在 的 房源"))
    assert action in {"no_match", "ambiguous"}
    with transaction() as connection:
        rows = connection.execute("SELECT * FROM feishu_sessions").fetchall()
    assert len(rows) == 1, "匹配失败时应保留待处理视频，让中介重发房源"


def test_cancel_clears_pending_video():
    client = prepare()
    agent = FeishuAgent(client)
    agent.handle_event(media_event("om_video_3"))
    action = agent.handle_event(text_event("om_text_3", "取消"))
    assert action == "cancel"
    with transaction() as connection:
        assert connection.execute("SELECT * FROM feishu_sessions").fetchall() == []


def test_render_command_guards():
    client = prepare()
    agent = FeishuAgent(client)
    assert agent.handle_event(text_event("om_t1", "出片")).startswith("render_no_task")
    assert agent.handle_event(text_event("om_t2", "出片 V20250101-ZZZZZ")).startswith("render_unknown_task")

    agent.handle_event(media_event("om_video_4"))
    agent.handle_event(text_event("om_t3", "A区 A01 101"))
    with transaction() as connection:
        task_no = connection.execute("SELECT task_no FROM tasks").fetchone()["task_no"]

    # 工作台地址为空时必须明确提示，而不是静默失败
    with transaction() as connection:
        connection.execute("UPDATE settings SET value='' WHERE key='workbench_url'")
    action = agent.handle_event(text_event("om_t4", f"出片 {task_no}"))
    assert action == "render_not_configured", action
    assert "工作台" in client.sent[-1][1]


def test_render_success_notifies_chat(monkeypatch, tmp_path):
    client = prepare()
    output = tmp_path / "final.mp4"
    output.write_bytes(b"\x00" * 32)

    def fake_render(task_id: str) -> None:
        with transaction() as connection:
            connection.execute(
                "UPDATE tasks SET status='completed', progress=100, output_video_path=? WHERE id=?",
                (str(output), task_id),
            )

    monkeypatch.setattr("app.feishu.handlers.render_task", fake_render)
    with transaction() as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES ('workbench_url','http://127.0.0.1:8765') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    agent = FeishuAgent(client)
    agent.handle_event(media_event("om_video_5"))
    agent.handle_event(text_event("om_t5", "A区 A01 101"))
    with transaction() as connection:
        task_no = connection.execute("SELECT task_no FROM tasks").fetchone()["task_no"]

    action = agent.handle_event(text_event("om_t6", f"出片 {task_no}"))
    assert action.startswith("render_started"), action

    deadline = time.time() + 10
    while time.time() < deadline:
        if any("出片完成" in text for _, text in client.sent):
            break
        time.sleep(0.05)
    assert any("出片完成" in text for _, text in client.sent), client.sent
    # 上传失败（测试里没有真实上传）时必须降级成路径提示，而不是静默
    assert any("final.mp4" in text for _, text in client.sent)


def test_status_and_help():
    client = prepare()
    agent = FeishuAgent(client)
    assert agent.handle_event(text_event("om_h1", "帮助")) == "help"
    assert "可用指令" in client.sent[-1][1]
    assert agent.handle_event(text_event("om_h2", "状态")) == "status_empty"
    assert agent.handle_event(text_event("om_h3", "房源 100平")) .startswith("search")


def test_duplicate_event_is_ignored():
    from app.feishu.handlers import claim_event

    prepare()
    assert claim_event("om_dup", "oc_test", "text") is True
    assert claim_event("om_dup", "oc_test", "text") is False


# ------------------------------------------------------------------ 客户端契约
def test_client_requires_credentials():
    client = FeishuClient(FeishuConfig())
    try:
        client._access_token()
    except Exception as error:  # FeishuError
        assert "FEISHU_APP_ID" in str(error)
    else:  # pragma: no cover
        raise AssertionError("缺少凭据时必须抛错")


def test_client_parses_media_content():
    from app.feishu.handlers import _parse_media, _parse_text

    key, name, duration = _parse_media('{"file_key": "abc12345", "file_name": "a.mp4", "duration": 5000}')
    assert key == "abc12345"
    assert name == "a.mp4"
    assert duration == 5000
    assert _parse_media('{"file_key": "abcdefgh"}')[1].endswith(".mp4")
    assert _parse_text('{"text": " 你好 "}') == "你好"
