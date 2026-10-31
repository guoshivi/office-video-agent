"""发布层回归测试。

重点不是"能不能发成功"（那需要真实账号），而是**发出去之前能不能拦住**：
标题超长、文案带微信、今日已超限、成片不存在——这些判断必须在不触碰任何
平台的前提下就准确，否则线上就是一次真实的风控事故。
"""

import httpx

from app.db import init_db, transaction
from app.publish import (
    _upsert_record,
    build_post,
    policy,
    publish_task,
    quota_state,
    short_title,
)
from app.publish.mcp_client import McpHttpClient, looks_like_failure


def make_task(**overrides):
    task = {
        "id": "task-test-1",
        "task_no": "V20261101-TEST1",
        "output_video_path": "",
        "script": "这套一百二十平，户型是店面，朝南，采光不错。",
        "region_norm": "南山科技园",
        "building_norm": "深港大厦",
        "room_norm": "1201",
        "area_sqm": 120.0,
        "area_raw": "120",
        "partition_raw": "3隔间",
        "orientation_raw": "朝南",
    }
    task.update(overrides)
    return task


def make_video(tmp_path, name="成片.mp4", size=4096):
    path = tmp_path / name
    path.write_bytes(b"\x00" * size)
    return path


# ---------- 标题压缩 ----------

def test_short_title_fits_xhs_limit():
    title = short_title(make_task())
    assert len(title) <= policy.XHS_TITLE_MAX
    assert "120平" in title


def test_short_title_drops_parts_instead_of_half_a_word():
    # 区域名很长时应该退到"楼栋+面积"，而不是把某一段砍成半个词
    title = short_title(make_task(region_norm="深圳市南山区高新技术产业园区北区"))
    assert len(title) <= policy.XHS_TITLE_MAX
    assert "平" in title


def test_short_title_falls_back_to_area_when_everything_is_long():
    title = short_title(
        make_task(region_norm="深圳市南山区高新技术产业园区北区", building_norm="国际创新中心大厦A座")
    )
    assert len(title) <= policy.XHS_TITLE_MAX
    assert title


# ---------- 路径 staging ----------

def test_stage_video_rewrites_chinese_name_to_ascii(tmp_path):
    source = make_video(tmp_path, "办公室 实拍 成片.mp4")
    staged = policy.stage_video(source, tmp_path / "stage", "V20261101-TEST1")
    assert policy.is_ascii_path(staged), f"staging 后仍是非 ASCII：{staged}"
    assert staged.exists() and staged.read_bytes() == source.read_bytes()


def test_stage_video_is_idempotent(tmp_path):
    source = make_video(tmp_path)
    first = policy.stage_video(source, tmp_path / "stage", "V1")
    second = policy.stage_video(source, tmp_path / "stage", "V1")
    assert first == second


# ---------- 规则校验 ----------

def test_wechat_in_copy_is_blocking(tmp_path):
    result = policy.check_xhs("南山 120平", "这套很划算，加微信 abc123 看房", make_video(tmp_path))
    assert result.ok is False
    assert any("微信" in issue.message for issue in result.blocking)


def test_phone_number_in_copy_is_blocking(tmp_path):
    result = policy.check_xhs("南山 120平", "详情 13800138000", make_video(tmp_path))
    assert result.ok is False


def test_private_message_is_only_a_warning(tmp_path):
    # 默认 public_contact 是「欢迎私信咨询」，不能被自己的规则挡死
    result = policy.check_xhs("南山 120平", "欢迎私信咨询房源详情和看房安排", make_video(tmp_path))
    assert result.ok is True


def test_absolute_words_warn_but_do_not_block(tmp_path):
    result = policy.check_xhs("南山 120平", "全城最低价，保证入驻", make_video(tmp_path))
    assert result.ok is True
    assert any("绝对化" in issue.message for issue in result.issues)


def test_long_copy_is_clipped(tmp_path):
    result = policy.check_xhs("标题", "办" * 1200, make_video(tmp_path))
    assert len(result.content) <= policy.XHS_CONTENT_MAX


def test_missing_video_blocks(tmp_path):
    result = policy.check_xhs("南山 120平", "正文", tmp_path / "nope.mp4")
    assert result.ok is False


# ---------- 文案组装 ----------

def test_build_post_appends_contact_from_settings():
    init_db()
    title, content, tags = build_post(make_task())
    assert title and content
    assert "私信" in content
    assert "办公室出租" in tags


# ---------- 频率控制 ----------

def test_quota_blocks_after_daily_limit():
    init_db()
    _upsert_record("task-quota", "xiaohongshu", {"status": "published", "title": "x"})
    state = quota_state("xiaohongshu", {"publish_daily_limit": "1", "publish_min_interval_minutes": "0"})
    assert state["used_today"] >= 1
    assert state["allowed"] is False


def test_quota_allows_first_post():
    init_db()
    state = quota_state("douyin", {"publish_daily_limit": "3", "publish_min_interval_minutes": "0"})
    assert state["allowed"] is True


# ---------- 记录唯一 ----------

def test_record_is_upserted_not_duplicated():
    init_db()
    _upsert_record("task-dup", "douyin", {"status": "publishing", "title": "a"})
    _upsert_record("task-dup", "douyin", {"status": "failed", "title": "b"})
    with transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM publish_records WHERE task_id = 'task-dup' AND platform = 'douyin'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["title"] == "b"


# ---------- 发布前置拦截 ----------

def test_publish_refuses_task_without_output():
    init_db()
    result = publish_task(make_task(), platforms=["xiaohongshu"])
    assert result["ok"] is False
    assert "成片" in result["message"]


def test_publish_blocks_on_policy_before_touching_platform(tmp_path):
    init_db()
    video = make_video(tmp_path)
    task = make_task(output_video_path=str(video), script="加微信 abc 看房")
    result = publish_task(task, platforms=["xiaohongshu"])
    assert result["ok"] is False
    assert "规则校验" in result["results"]["xiaohongshu"]["message"]


# ---------- MCP 协议解析 ----------

def test_mcp_parses_sse_response():
    client = McpHttpClient("http://127.0.0.1:1/mcp")
    try:
        response = httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=': keepalive\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n',
        )
        assert client._parse(response)["result"]["ok"] is True
    finally:
        client.close()


def test_mcp_parses_json_response():
    client = McpHttpClient("http://127.0.0.1:1/mcp")
    try:
        response = httpx.Response(
            200, headers={"Content-Type": "application/json"}, json={"result": {"tools": []}}
        )
        assert client._parse(response)["result"] == {"tools": []}
    finally:
        client.close()


def test_failure_detection_covers_service_wording():
    assert looks_like_failure("❌ 未登录\n\n请使用 get_login_qrcode") is True
    assert looks_like_failure("发布失败：视频过大") is True
    assert looks_like_failure("✅ 发布成功") is False
    assert looks_like_failure("") is False
