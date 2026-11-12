"""任务级观感覆盖：设置是默认，任务是例外。

竖屏要的字号比横屏大、长文案要的语速比短文案快。这类差异每条视频都会变，
所以提到任务上；BGM、运镜、花字风格一次调好就不动，留在设置里。

这里锁住三件事：
1. 任务填了就用任务的，没填就回落到设置；
2. 不认识的值当没填，不写进库；
3. 本机 ffmpeg 和远程工作台两条出片路径都得认这些值——只改一条等于没改。
"""

from __future__ import annotations

import pytest

from app.db import init_db, transaction
from app.style_render import CAPTION_BOTTOM_CHOICES, CAPTION_SIZE_CHOICES, _local_audio_visual_settings
from app.workbench_client import _render_settings


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'render_%'")
    yield


def _task(**overrides) -> dict:
    base = {
        "aspect_ratio": "9:16",
        "voice": "female_warm",
        "target_duration": 45,
        "source_video_path": "",
        "task_no": "V20260101-AAAAA",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 本机出片


def test_local_falls_back_to_settings():
    visual = _local_audio_visual_settings("V1", _task())
    assert visual["caption_size"] == CAPTION_SIZE_CHOICES["medium"]
    assert visual["caption_bottom"] == CAPTION_BOTTOM_CHOICES["middle"]
    assert visual["flower"] is True
    assert visual["tts_rate"] == ""


def test_local_task_size_wins():
    visual = _local_audio_visual_settings("V1", _task(subtitle_size="large"))
    assert visual["caption_size"] == CAPTION_SIZE_CHOICES["large"]


def test_local_task_bottom_wins():
    visual = _local_audio_visual_settings("V1", _task(subtitle_bottom="high"))
    assert visual["caption_bottom"] == CAPTION_BOTTOM_CHOICES["high"]


def test_local_task_rate_wins_over_blank_setting():
    visual = _local_audio_visual_settings("V1", _task(tts_rate="+16%"))
    assert visual["tts_rate"] == "+16%"


def test_local_task_can_turn_flower_off():
    visual = _local_audio_visual_settings("V1", _task(flower="0"))
    assert visual["flower"] is False


def test_local_flower_follows_setting_when_task_blank():
    with transaction() as connection:
        connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('render_flower', '0')")
    assert _local_audio_visual_settings("V1", _task())["flower"] is False
    # 任务上明确写 1，就该压过设置里的关
    assert _local_audio_visual_settings("V1", _task(flower="1"))["flower"] is True


def test_local_unknown_value_falls_back():
    # 手改库或者老数据写进奇怪的值，不能让它把出片搞崩
    visual = _local_audio_visual_settings("V1", _task(subtitle_size="huge"))
    assert visual["caption_size"] == CAPTION_SIZE_CHOICES["medium"]


# ---------------------------------------------------------------- 远程工作台


def test_remote_task_size_wins():
    payload = _render_settings(_task(subtitle_size="small"))
    assert payload["subtitle_size"] == 42


def test_remote_task_rate_wins():
    payload = _render_settings(_task(tts_rate="+16%"))
    assert payload["speed"] == pytest.approx(1.16)


def test_remote_task_can_turn_flower_off():
    assert _render_settings(_task(flower="0"))["flower_enabled"] is False


def test_remote_safe_mode_still_kills_decorations():
    # 失败重跑的安全模式优先级最高：任务上选了开花字也得关
    assert _render_settings(_task(flower="1"), safe_mode=True)["flower_enabled"] is False


# ---------------------------------------------------------------- 落库


def test_store_and_read_back():
    from app.services import set_task_render_options

    init_db()
    with transaction() as connection:
        connection.execute(
            "INSERT INTO properties(id, created_at, updated_at)"
            " VALUES ('p1', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            """INSERT INTO tasks(id, task_no, property_id, source, original_filename,
               source_video_path, target_duration, aspect_ratio, voice, style, script,
               warnings_json, status, created_at, updated_at)
               VALUES ('t1', 'V1', 'p1', 'webui', 'a.mp4', '', 45, '9:16',
                       'female_warm', 'professional', '文案。', '[]', 'script_ready',
                       '2026-01-01', '2026-01-01')"""
        )
    assert set_task_render_options(
        "t1", subtitle_size="large", subtitle_bottom="low", tts_rate="+8%", flower="0"
    )
    with transaction() as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id='t1'").fetchone()
    assert row["subtitle_size"] == "large"
    assert row["subtitle_bottom"] == "low"
    assert row["tts_rate"] == "+8%"
    assert row["flower"] == "0"


def test_store_drops_unknown_values():
    from app.services import set_task_render_options

    init_db()
    with transaction() as connection:
        connection.execute(
            "INSERT INTO properties(id, created_at, updated_at)"
            " VALUES ('p2', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            """INSERT INTO tasks(id, task_no, property_id, source, original_filename,
               source_video_path, target_duration, aspect_ratio, voice, style, script,
               warnings_json, status, created_at, updated_at)
               VALUES ('t2', 'V2', 'p2', 'webui', 'a.mp4', '', 45, '9:16',
                       'female_warm', 'professional', '文案。', '[]', 'script_ready',
                       '2026-01-01', '2026-01-01')"""
        )
    set_task_render_options("t2", subtitle_size="giant", flower="yes")
    with transaction() as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id='t2'").fetchone()
    # 认不出的值一律写空，出片时自然回落设置，而不是带着脏值去渲染
    assert row["subtitle_size"] == ""
    assert row["flower"] == ""
