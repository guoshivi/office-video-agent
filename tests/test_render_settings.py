"""出片参数必须跟工作台界面自己的默认值对齐。

这几项直接被中介拿来对比：「为什么用你的工具出的片，不如我直接在 8765 上点？」
历史坑：贴纸/花字/音效被关掉、dedup 开了 light、shot_reuse 设成 2、字幕低了 20px，
单看每项都像小事，叠起来就是「一眼廉价」。
"""

from __future__ import annotations

import pytest

from app.db import init_db, transaction
from app.workbench_client import _render_settings


@pytest.fixture(autouse=True)
def _clean():
    """每个用例都从干净状态开始：建表并清掉出片开关。"""
    init_db()
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'render_%'")
    yield

TASK = {
    "aspect_ratio": "9:16",
    "voice": "female_warm",
    "target_duration": 30,
    "source_video_path": "",
}


def test_decorations_follow_workbench_defaults():
    payload = _render_settings(TASK)
    assert payload["sticker_enabled"] is True
    assert payload["flower_enabled"] is True
    assert payload["sfx_enabled"] is True
    assert payload["flower_style"] == "auto"
    assert payload["flower_density"] == "high"


def test_safe_mode_turns_decorations_off():
    payload = _render_settings(TASK, safe_mode=True)
    assert payload["sticker_enabled"] is False
    assert payload["flower_enabled"] is False
    assert payload["sfx_enabled"] is False


def test_subtitle_and_shot_params_match_workbench():
    payload = _render_settings(TASK)
    assert payload["subtitle_bottom"] == 380
    assert payload["subtitle_style"] == "classic"
    assert payload["highlight_source"] == "ai"
    assert payload["subtitle_highlight"] is True
    # 开 light 会为躲判重动画面，画质反而掉；-1 会让同段画面重复出现
    assert payload["dedup"] == "off"
    assert payload["shot_reuse"] == 1


def test_flat_look_options_stay_off():
    payload = _render_settings(TASK)
    assert payload["beauty"] is False
    assert payload["visual_filter"] == "original"
    assert payload["bgm"] is False
    assert payload["logo_enabled"] is False
    assert payload["watermark_enabled"] is False


def test_portrait_canvas():
    payload = _render_settings(TASK)
    assert (payload["width"], payload["height"]) == (1080, 1920)
    assert payload["fps"] == 30
