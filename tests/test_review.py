"""审核台测试：重点是"拦住错的东西"，而不是"能跑通"。

真实场景里最贵的错误是这三种：面积说错了、串了别的楼栋、文案带微信号。
这三条必须在没有人盯着的情况下也能拦住。
"""

from app.db import DATA_DIR, init_db, transaction
from app.review import approve, detail, list_queue, precheck, refresh_queue, reject, store
from app.review.precheck import cn_to_int, extract_areas, extract_partitions, extract_rents
from app.review.service import ReviewError

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _prepare_db():
    """每个用例都从干净的队列开始。

    不清空会出问题：同一个 task_id 会被上一个用例的审核结论和预检结果带过来，
    表现出来就是"单独跑通过、一起跑失败"。
    """
    init_db()
    with transaction() as connection:
        for table in ("review_items", "publish_records", "tasks"):
            connection.execute(f"DELETE FROM {table}")
    yield


def make_task(**overrides):
    task = {
        "id": "review-task-1",
        "task_no": "V20261101-REV1",
        "property_id": "prop-1",
        "output_video_path": "",
        "script": "这套一百二十平，户型是店面，朝南。",
        "region_norm": "南山科技园",
        "building_norm": "深港大厦",
        "room_norm": "1201",
        "area_sqm": 120.0,
        "area_raw": "120",
        "rent_raw": "12000元/月",
        "partition_raw": "3隔间",
    }
    task.update(overrides)
    return task


def seed(script="这套一百二十平，户型是店面，朝南。", task_id="review-task-1", **overrides):
    """建一条任务 + 房源 + 一个占位成片，让审核台有东西可审。"""
    init_db()
    video_dir = DATA_DIR / "review-test"
    video_dir.mkdir(parents=True, exist_ok=True)
    video = video_dir / f"{task_id}.mp4"
    video.write_bytes(b"\x00" * 2048)

    with transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO properties (id, match_key, region_norm, building_norm, "
            "room_norm, area_raw, area_sqm, rent_raw, partition_raw, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "prop-1", "南山科技园|深港大厦|1201", "南山科技园", "深港大厦",
                "1201", "120", 120.0, "12000元/月", "3隔间", "2026-11-01", "2026-11-01",
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO tasks (id, task_no, property_id, original_filename, "
            "source_video_path, script, status, output_video_path, created_at, updated_at) "
            "VALUES (?, ?, 'prop-1', 'a.mp4', 'a.mp4', ?, 'completed', ?, '2026-11-01', '2026-11-01')",
            (task_id, "V20261101-REV1", script, str(video)),
        )
    return task_id


# ---------- 中文数字 ----------

def test_cn_to_int_common_forms():
    assert cn_to_int("一百二十") == 120
    assert cn_to_int("三百") == 300
    assert cn_to_int("五十") == 50
    assert cn_to_int("十五") == 15
    assert cn_to_int("两千") == 2000
    assert cn_to_int("不认识的") is None


def test_extract_areas_handles_both_notations():
    assert 120.0 in extract_areas("这套一百二十平")
    assert 120.0 in extract_areas("面积120㎡")
    assert 85.5 in extract_areas("85.5 平方米")


def test_extract_rents_normalises_wan():
    values = [value for value, _ in extract_rents("月租一万二")]
    assert 12000 in values
    values = [value for value, _ in extract_rents("1.2万元/月")]
    assert 12000 in values


def test_extract_partitions():
    assert 3 in extract_partitions("3隔间")
    assert 4 in extract_partitions("4个隔间")


# ---------- 预检：事实 ----------

def test_area_mismatch_is_blocking():
    report = precheck(make_task(script="这套一百五十平，朝南。"))
    assert report.level == "blocking"
    assert any(check.id == "area" and check.level == "blocking" for check in report.checks)


def test_area_within_tolerance_passes():
    report = precheck(make_task(script="这套一百二十平，朝南。"))
    area = [check for check in report.checks if check.id == "area"][0]
    assert area.level == "ok"


def test_wrong_building_is_blocking():
    init_db()
    with transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO properties (id, match_key, building_norm, created_at, updated_at) "
            "VALUES ('prop-2', 'x|吉比特大楼|101', '吉比特大楼', '2026-11-01', '2026-11-01')"
        )
    report = precheck(make_task(script="这套在吉比特大楼，一百二十平。"))
    identity = [check for check in report.checks if check.id == "identity"][0]
    assert identity.level == "blocking"
    assert "吉比特大楼" in identity.detail


def test_wechat_in_script_is_blocking():
    report = precheck(make_task(script="这套一百二十平，加微信 abc 看房。"))
    assert any(
        check.id == "compliance" and check.level == "blocking" for check in report.checks
    )


def test_missing_output_is_blocking():
    report = precheck(make_task())
    assert any(check.id == "video" and check.level == "blocking" for check in report.checks)


# ---------- 队列与决策 ----------

def test_refresh_enqueues_completed_task():
    seed()
    result = refresh_queue(auto_precheck=False)
    assert result["enqueued"] >= 1
    queue = list_queue()
    assert any(item["task_id"] == "review-task-1" for item in queue["items"])


def test_approve_blocked_when_precheck_has_blocking():
    seed(script="这套一百五十平，加微信 abc。")
    refresh_queue(auto_precheck=True)
    with pytest.raises(ReviewError) as error:
        approve("review-task-1")
    assert "必须处理" in str(error.value)


def test_approve_can_override_risky_with_flag():
    seed(script="这套一百五十平。")
    refresh_queue(auto_precheck=True)
    result = approve("review-task-1", confirm_risky=True, note="面积口径确认过")
    assert result["ok"] is True
    assert detail("review-task-1")["status"] == "approved"


def test_approve_clean_task_passes():
    seed(script="这套一百二十平，深港大厦，朝南。")
    refresh_queue(auto_precheck=True)
    result = approve("review-task-1", reviewer="老板")
    assert result["status"] == "approved"
    assert detail("review-task-1")["reviewer"] == "老板"


def test_reject_requires_reason():
    seed()
    refresh_queue(auto_precheck=False)
    with pytest.raises(ReviewError):
        reject("review-task-1")
    assert reject("review-task-1", note="画面太暗")["status"] == "rejected"


def test_store_create_is_idempotent():
    seed()
    first = store.create("review-task-1")
    second = store.create("review-task-1")
    assert first == second


# ---------- 多维表格桥接 ----------
# 桥接是没有网络也能验的部分：给它一个假客户端，检查"表格上的裁决"能不能正确
# 落到本地状态机上。真实的飞书接口留给联调时验。

class FakeBitable:
    """只实现 pull 用得着的那一个方法，行为对齐 FeishuClient.bitable_search。"""

    def __init__(self, records):
        self.records = records
        self.config = SimpleNamespace(task_table_id="tbl-fake", has_bitable=True)

    def bitable_search(self, table_id, field_name, value):
        from app.review.bitable_bridge import _field_text

        return [
            record
            for record in self.records
            if _field_text(record["fields"].get(field_name)) == value
        ]


def test_bitable_push_skips_without_credentials(monkeypatch):
    from app.review import bitable_bridge

    monkeypatch.setattr(bitable_bridge, "_client", lambda: None)
    seed()
    refresh_queue(auto_precheck=False)

    assert bitable_bridge.available() is False
    assert bitable_bridge.push_item("review-task-1") == "skipped"
    result = bitable_bridge.pull_decisions()
    assert result["available"] is False
    assert bitable_bridge.push_all()["skipped"] >= 1


def test_bitable_pull_applies_approval(monkeypatch):
    from app.review import bitable_bridge

    seed(script="这套一百二十平，深港大厦，朝南。")
    refresh_queue(auto_precheck=True)

    fake = FakeBitable(
        [
            {
                "record_id": "rec1",
                "fields": {
                    "任务编号": "V20261101-REV1",
                    "审核状态": "通过",
                    "审核人": "老板",
                    "驳回原因": "",
                },
            }
        ]
    )
    monkeypatch.setattr(bitable_bridge, "_client", lambda: fake)

    result = bitable_bridge.pull_decisions()
    assert result["available"] is True
    assert result["approved"] == 1
    item = detail("review-task-1")
    assert item["status"] == "approved"
    assert item["reviewer"] == "老板"


def test_bitable_pull_does_not_override_local_decision(monkeypatch):
    from app.review import bitable_bridge

    seed(script="这套一百二十平，深港大厦，朝南。")
    refresh_queue(auto_precheck=True)
    approve("review-task-1", reviewer="在电脑上审的人")

    fake = FakeBitable(
        [
            {
                "record_id": "rec1",
                "fields": {"任务编号": "V20261101-REV1", "审核状态": "驳回", "驳回原因": "旧的"},
            }
        ]
    )
    monkeypatch.setattr(bitable_bridge, "_client", lambda: fake)

    result = bitable_bridge.pull_decisions()
    assert result["rejected"] == 0
    assert result["skipped"] == 1
    assert detail("review-task-1")["status"] == "approved"


def test_bitable_pull_reject_without_reason_still_lands(monkeypatch):
    """表格里驳回但没填原因：本地流程不能因为"没原因"卡住，要给个占位。"""
    from app.review import bitable_bridge

    seed()
    refresh_queue(auto_precheck=False)

    fake = FakeBitable(
        [{"record_id": "rec1", "fields": {"任务编号": "V20261101-REV1", "审核状态": "驳回"}}]
    )
    monkeypatch.setattr(bitable_bridge, "_client", lambda: fake)

    result = bitable_bridge.pull_decisions()
    assert result["rejected"] == 1
    item = detail("review-task-1")
    assert item["status"] == "rejected"
    assert "多维表格" in item["note"]


def test_bitable_field_text_handles_shapes():
    from app.review.bitable_bridge import _field_text

    assert _field_text("  通过 ") == "通过"
    assert _field_text([{"text": "通"}, {"text": "过"}]) == "通过"
    assert _field_text({"text": "驳回"}) == "驳回"
    assert _field_text({"name": "老板"}) == "老板"
    assert _field_text(None) == ""
    assert _field_text(120) == "120"


def test_bitable_search_all_pages_until_exhausted():
    """表格里标成「通过」的记录超过一页时，第二页也必须拉到。

    这是真实会踩的坑：FeishuClient.bitable_search 只取第一页 20 条，
    审核裁决同步靠它的话，第 21 条记录永远同步不回来。
    """
    from app.feishu.client import FeishuConfig, FeishuClient

    client = FeishuClient(FeishuConfig(app_id="x", app_secret="y", base_app_token="b", task_table_id="t"))
    calls = []

    def fake_request(method, path, *, json_body=None, params=None, timeout=30):
        calls.append(params)
        if len(calls) == 1:
            return {"data": {"items": [{"record_id": "a"}], "page_token": "p2", "has_more": True}}
        return {"data": {"items": [{"record_id": "b"}], "has_more": False}}

    client._request = fake_request  # type: ignore[assignment]
    items = client.bitable_search_all("tbl", "审核状态", "通过", page_size=1)

    assert [item["record_id"] for item in items] == ["a", "b"]
    assert "page_token" not in calls[0]
    assert calls[1]["page_token"] == "p2"


def test_bitable_search_all_stops_at_max_pages():
    """表格出问题一直返回 has_more 时，靠 max_pages 兜住，不能把轮询线程卡死。"""
    from app.feishu.client import FeishuConfig, FeishuClient

    client = FeishuClient(FeishuConfig(app_id="x", app_secret="y", base_app_token="b", task_table_id="t"))
    calls = []

    def fake_request(method, path, *, json_body=None, params=None, timeout=30):
        calls.append(1)
        return {"data": {"items": [{"record_id": "a"}], "page_token": "same", "has_more": True}}

    client._request = fake_request  # type: ignore[assignment]
    items = client.bitable_search_all("tbl", "审核状态", "通过", max_pages=3)

    assert len(items) == 3
    assert len(calls) == 3
