"""默认时长 = 跟原片。

45 秒是个凭空猜的数：素材 25 秒撑不满，60 秒又会被剪碎。
「跟原片」不用猜，但 0 这个哨兵值必须在建任务时就换成真实秒数——
否则写脚本那句 `max(10, target_duration or 45)` 会把 0 当 45 秒去算句子数，
结果句子数跟画面长度对不上。
"""

from __future__ import annotations

import pytest

import app.shots as shots
from app.db import init_db, transaction
from app.services import _resolve_target_duration


def _read(key: str) -> str:
    with transaction() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def _write(key: str, value: str) -> None:
    with transaction() as connection:
        connection.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


# ---------------------------------------------------------------- 默认值

def test_fresh_install_defaults_to_follow_source():
    init_db()
    assert _read("default_duration") == "0"


def test_old_library_on_45_seconds_switches_to_follow_source():
    init_db()
    _write("default_duration", "45")
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key='migration_default_duration_source'")

    init_db()

    assert _read("default_duration") == "0"


def test_migration_keeps_a_deliberate_custom_length():
    init_db()
    _write("default_duration", "30")
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key='migration_default_duration_source'")

    init_db()

    assert _read("default_duration") == "30"


# ---------------------------------------------------------------- 0 的解析

def test_explicit_length_is_left_alone(monkeypatch):
    # 填了秒数就不该去探素材，白花一次 ffprobe
    monkeypatch.setattr(shots, "probe_duration", lambda path: pytest.fail("不该探测"))
    assert _resolve_target_duration(30, "whatever.mp4") == 30


def test_zero_resolves_to_source_seconds(monkeypatch):
    monkeypatch.setattr(shots, "probe_duration", lambda path: 37.4)
    assert _resolve_target_duration(0, "source.mp4") == 37


def test_very_short_source_clamps_to_ten_seconds(monkeypatch):
    # 3 秒素材出 3 秒片子没有信息量，尾帧定格补到 10 秒下限
    monkeypatch.setattr(shots, "probe_duration", lambda path: 3.2)
    assert _resolve_target_duration(0, "source.mp4") == 10


def test_very_long_source_clamps_to_upper_bound(monkeypatch):
    monkeypatch.setattr(shots, "probe_duration", lambda path: 600.0)
    assert _resolve_target_duration(0, "source.mp4") == 180


def test_unreadable_source_falls_back_to_45(monkeypatch):
    def boom(path):
        raise shots.ShotError("读不出视频时长")

    monkeypatch.setattr(shots, "probe_duration", boom)
    assert _resolve_target_duration(0, "broken.mp4") == 45
