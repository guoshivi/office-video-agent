"""分镜：一句一镜的合并与校验。

不依赖 ffmpeg —— 这里只测「文案怎么合成」「超长怎么拦」，
抽帧本身在真实机器上跑 analyze 验证。
"""

import json
import os
import tempfile

os.environ.setdefault("OVA_DATA_DIR", tempfile.mkdtemp(prefix="office-video-agent-shots-"))

import pytest

from app.channel import bind_clip, save_clip
from app.shots import SHOT_DIR, ShotError, compose_script, save_storyboard
from test_feishu import prepare


def make_task() -> dict:
    prepare()
    from io import BytesIO

    clip = save_clip("实拍.mp4", BytesIO(b"\x00" * 2048), channel="desktop", sender="me")
    result = bind_clip(clip["id"], query="A区 A01 202-2")
    task = result["task"]
    # 伪造抽帧结果，避免测试依赖 ffmpeg
    shot_dir = SHOT_DIR / task["task_no"]
    shot_dir.mkdir(parents=True, exist_ok=True)
    (shot_dir / "shots.json").write_text(
        json.dumps({"task_no": task["task_no"], "shots": [], "storyboard": []}),
        encoding="utf-8",
    )
    return task


def test_compose_script_one_sentence_per_shot():
    script = compose_script([
        {"index": 3, "text": "第三句"},
        {"index": 1, "text": "第一句。"},
        {"index": 2, "text": ""},
        {"index": 4, "text": "第四句？"},
    ])
    # 按 index 排、空句丢掉、缺句号补上
    assert script == "第一句。第三句。第四句？"


def test_storyboard_overwrites_script():
    task = make_task()
    result = save_storyboard(task["id"], [
        {"index": 1, "label": "门头", "text": "F区F14，120平店面"},
        {"index": 2, "label": "工位区", "text": "大开间，二十个工位放得下"},
    ])
    assert result["sentence_count"] == 2
    assert result["script"].endswith("放得下。")
    assert result["estimated_seconds"] > 0

    from app.services import get_task

    stored = get_task(task["id"])
    assert stored["script"] == result["script"]
    assert "门头" in stored["storyboard_json"]


def test_storyboard_rejects_long_sentence():
    task = make_task()
    with pytest.raises(ShotError, match="超过一屏字幕"):
        save_storyboard(task["id"], [
            {"index": 1, "text": "这是一句特别长的话" * 4},
        ])


def test_storyboard_requires_content():
    task = make_task()
    with pytest.raises(ShotError):
        save_storyboard(task["id"], [{"index": 1, "text": "  "}])
