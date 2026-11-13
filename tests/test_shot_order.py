"""镜头排布：走位循环 + 同一画面只留一次。

修的是一个看得见的问题：10 秒素材出 30 秒片子，以前排出来是
「茶桌、工位、茶桌、工位」——三秒一次在同一面墙上来回横跳，
观众以为卡住了。现在改成一轮走完再回门口，相邻两镜永远不是同一处。

另一个是素材自己的问题：一镜到底的切点常把同一张桌子切成两段相邻机位，
两段连用等于复读。用 16×16 灰度指纹做相似度，超过 SIMILAR 只留分高的。

只测纯函数，不依赖 ffmpeg。
"""

from __future__ import annotations

from app.shot_select import SIMILAR, _distinct, _lay_clips, similarity


def clip(start: float, *, score: float = 1.0, sig: list[float] | None = None,
         seconds: float = 3.0) -> dict:
    return {
        "start": start,
        "end": start + seconds,
        "seconds": seconds,
        "score": score,
        "detail": 0.3,
        "has_text": False,
        "sig": sig,
    }


def pattern(seed: int, size: int = 256) -> list[float]:
    """造一个确定性的指纹：同一 seed 旋转不同角度，模拟「不同房间」。"""
    values = [((i * (seed + 1)) % 17) - 8.0 for i in range(size)]
    mean = sum(values) / size
    centered = [v - mean for v in values]
    std = (sum(v * v for v in centered) / size) ** 0.5 or 1.0
    return [round(v / std, 2) for v in centered]


# ------------------------------------------------------------------ 相似度

def test_missing_signature_never_blocks():
    # 老缓存里没有 sig：不能因为缺指纹就把所有段都当成重复
    assert similarity(None, pattern(1)) == 0.0
    assert similarity([], []) == 0.0


def test_same_signature_is_one():
    sig = pattern(3)
    assert similarity(sig, sig) > 0.99


def test_threshold_only_catches_the_same_frame():
    # 0.98 是同一帧，0.71 是同一张桌子的另一个机位——阈值必须夹在中间，
    # 判高了会把不同画面当重复删掉
    assert 0.71 < SIMILAR < 0.98


def test_same_room_from_another_angle_is_kept():
    # 同一间房换个角度（相关 0.71）不是重复：删了就把房间拍没了
    import math

    base = pattern(1)
    other = pattern(2)
    mixed = [round(0.71 * a + math.sqrt(1 - 0.71 ** 2) * b, 2) for a, b in zip(base, other)]
    assert 0.6 < similarity(base, mixed) < SIMILAR
    assert len(_distinct([clip(0.0, sig=base), clip(3.0, sig=mixed)])) == 2


# ------------------------------------------------------------------ 同一画面只留一次

def test_similar_clip_is_dropped():
    base = pattern(1)
    # 同一画面：指纹几乎一样，分数低的那个该被丢掉
    near = [v * 0.98 + 0.02 for v in base]
    clips = [clip(0.0, score=0.5, sig=base), clip(3.0, score=0.4, sig=near)]
    kept = _distinct(clips)
    assert len(kept) == 1
    assert kept[0]["score"] == 0.5      # 留分高的


def test_different_clips_all_kept():
    clips = [clip(0.0, sig=pattern(1)), clip(3.0, sig=pattern(2)), clip(6.0, sig=pattern(3))]
    assert len(_distinct(clips)) == 3


def test_distinct_keeps_order_by_score_then_caller_sorts():
    # 去重按分数从高到低看，返回的顺序也按分数——排序交给调用方
    clips = [clip(0.0, score=0.3, sig=pattern(1)), clip(3.0, score=0.9, sig=pattern(2))]
    assert [c["start"] for c in _distinct(clips)] == [3.0, 0.0]


# ------------------------------------------------------------------ 走位循环

def test_cycle_never_repeats_neighbour():
    clips = [clip(0.0, sig=pattern(1)), clip(3.0, sig=pattern(2)), clip(6.0, sig=pattern(3))]
    laid = _lay_clips(clips, 9)
    assert len(laid) == 9
    for i in range(len(laid) - 1):
        assert laid[i] != laid[i + 1], f"第 {i + 1} 镜和第 {i + 2} 镜是同一个画面"


def test_cycle_walks_the_floor_then_starts_over():
    clips = [clip(0.0), clip(3.0), clip(6.0)]
    laid = _lay_clips(clips, 6)
    # 一轮走完再回起点，而不是 A、B、A、B 来回跳；
    # 第二轮入点各挪 0.45 秒，观众看到的不是原样重播
    assert [start for start, _end in laid] == [0.0, 3.0, 6.0, 0.45, 3.45, 6.45]


def test_second_use_offsets_the_in_point():
    # 同一段第二次用，入点往后挪：别让观众看到原样重播
    clips = [clip(0.0, seconds=3.0)]
    laid = _lay_clips(clips, 2)
    assert laid[0] == (0.0, 3.0)
    assert laid[1][0] > 0.0
    assert laid[1][1] == 3.0


def test_offset_stops_before_clip_gets_too_short():
    # 3 秒的段挪到 2.6 秒就停：再挪下去这段只剩不到 MIN_CLIP，画面会卡
    laid = _lay_clips([clip(0.0, seconds=1.8)], 4)
    assert all(end - start >= 1.4 - 0.01 for start, end in laid)


def test_empty_is_safe():
    assert _lay_clips([], 5) == []
