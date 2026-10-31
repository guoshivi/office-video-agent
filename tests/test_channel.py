"""多入口收片：先存视频、后绑房源这条路径的回归测试。

WebUI、WorkBuddy 桌面、飞书都走 channel.py，所以这里测的是三个入口的共同地基：
素材不能丢、歧义不能猜、绑定后不能留垃圾文件。
"""

import os
import tempfile
from io import BytesIO

os.environ.setdefault("OVA_DATA_DIR", tempfile.mkdtemp(prefix="office-video-agent-channel-"))

import pytest

from app.channel import CLIP_DIR, ChannelError, bind_clip, drop_clip, get_clip, latest_pending, save_clip
from app.db import transaction
from test_feishu import prepare


def upload(data: bytes = b"\x00" * 4096, name: str = "办公室实拍.mp4", channel: str = "desktop", sender: str = "me"):
    return save_clip(name, BytesIO(data), channel=channel, sender=sender)


def property_id_of(room: str) -> str:
    with transaction() as connection:
        return connection.execute(
            "SELECT id FROM properties WHERE room_norm=? ORDER BY source_row LIMIT 1", (room,)
        ).fetchone()["id"]


def test_clip_is_staged_before_property_is_known():
    prepare()
    clip = upload()
    assert clip["status"] == "pending"
    assert clip["size"] == 4096
    assert (CLIP_DIR / clip["id"]).is_dir()
    assert latest_pending("desktop", "me")["id"] == clip["id"]
    # 别的渠道不串台
    assert latest_pending("feishu", "me") == {}


def test_ambiguous_property_keeps_clip_pending():
    prepare()
    clip = upload()
    result = bind_clip(clip["id"], query="102")
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    # 没绑定成功就不能动素材，也不能建任务
    assert get_clip(clip["id"])["status"] == "pending"
    assert (CLIP_DIR / clip["id"]).is_dir()
    with transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_bind_creates_task_and_cleans_inbox():
    prepare()
    clip = upload()
    result = bind_clip(clip["id"], query="A区 A01 202-2")
    assert result["status"] == "bound"

    task = result["task"]
    assert task["property_label"].endswith("202-2")
    assert task["source"] == "desktop"
    assert task["script"], "口播草稿不能为空"

    bound = get_clip(clip["id"])
    assert bound["status"] == "bound"
    assert bound["task_id"] == task["id"]
    # 素材已经搬进任务目录，inbox 里不该再留一份
    assert not (CLIP_DIR / clip["id"]).exists()
    assert latest_pending("desktop", "me") == {}

    # 重复绑定要幂等，不能建第二个任务
    again = bind_clip(clip["id"], query="A区 A01 202-2")
    assert again["status"] == "already_bound"
    assert again["task"]["task_no"] == task["task_no"]


def test_explicit_property_id_skips_parsing():
    prepare()
    clip = upload()
    result = bind_clip(clip["id"], property_id=property_id_of("202-2"))
    assert result["status"] == "bound"
    assert result["task"]["property_label"].endswith("202-2")


def test_drop_removes_file_and_blocks_binding():
    prepare()
    clip = upload()
    assert drop_clip(clip["id"]) is True
    assert not (CLIP_DIR / clip["id"]).exists()
    with pytest.raises(ChannelError):
        bind_clip(clip["id"], query="A区 A01 101")


def test_rejects_non_video_extension():
    prepare()
    with pytest.raises(ChannelError):
        save_clip("方案.docx", BytesIO(b"x"), channel="desktop")


def test_rejects_empty_upload():
    prepare()
    with pytest.raises(ChannelError):
        save_clip("空.mp4", BytesIO(b""), channel="desktop")
