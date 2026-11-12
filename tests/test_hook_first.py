"""开场镜头：第一眼必须是信息量最大的那一段。

抖音的流量几乎由前 3 秒决定。原来的选镜最后一步是按拍摄时间排序，
等于「片子里最早那一秒开场」——哪怕那是一面白墙、一段走廊。
现在把信息量最高的一段提到最前，其余仍按拍摄顺序绕一圈，
保住「进门往里走」的空间感。

这里只测排序逻辑（不依赖 ffmpeg）。
"""

from __future__ import annotations

from app.shot_select import _hook_order


def clip(start: float, detail: float, *, score: float = 1.0, has_text: bool = False) -> dict:
    return {
        "start": start,
        "end": start + 3.0,
        "seconds": 3.0,
        "score": score,
        "detail": detail,
        "has_text": has_text,
    }


def test_empty_is_safe():
    assert _hook_order([]) == []


def test_single_clip_untouched():
    one = [clip(0.0, 0.3)]
    assert _hook_order(one) == one


def test_most_detail_goes_first():
    clips = [clip(0.0, 0.30), clip(3.0, 0.85), clip(6.0, 0.45)]
    order = [c["start"] for c in _hook_order(clips)]
    assert order[0] == 3.0


def test_rest_keep_shooting_order():
    # 选中 3.0 开场后，后面的顺序仍是 6.0 → 0.0（绕一圈），没有打乱
    clips = [clip(0.0, 0.30), clip(3.0, 0.85), clip(6.0, 0.45)]
    order = [c["start"] for c in _hook_order(clips)]
    assert order == [3.0, 6.0, 0.0]


def test_tail_clip_never_opens():
    # 最后 15% 不参与开场：从片尾开场再绕回门口，观感是倒退
    clips = [clip(i * 3.0, 0.5) for i in range(10)]
    clips[-1]["detail"] = 1.0
    order = [c["start"] for c in _hook_order(clips)]
    assert order[0] != clips[-1]["start"]


def test_text_frames_lose_to_clean_ones():
    # 有字幕/角标的画面不上开场，哪怕边缘更密
    clips = [clip(0.0, 0.2), clip(3.0, 0.95, has_text=True), clip(6.0, 0.6)]
    order = [c["start"] for c in _hook_order(clips)]
    assert order[0] == 6.0


def test_disabled_keeps_shooting_order():
    clips = [clip(0.0, 0.3), clip(3.0, 0.9), clip(6.0, 0.4)]
    order = [c["start"] for c in _hook_order(clips, hook_first=False)]
    assert order == [0.0, 3.0, 6.0]


def test_missing_detail_falls_back_to_score():
    # 老缓存里没有 detail 字段：不能抛错，退回按画质分挑
    clips = [
        {"start": 0.0, "end": 3.0, "score": 0.6, "has_text": False},
        {"start": 3.0, "end": 6.0, "score": 0.9, "has_text": False},
    ]
    order = [c["start"] for c in _hook_order(clips)]
    assert order[0] == 3.0
