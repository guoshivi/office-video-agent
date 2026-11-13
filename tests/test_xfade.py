"""镜头叠化：转场要「吃」出总长，但一分不能多也不能少。

xfade 的坑：叠化区间里两路画面同时消耗，总长 = Σ段长 − Σ叠化。
所以每段要预存余量 extra，叠化从上一段窗口末开始吃——
音轨、字幕的时间轴才能原样不动。这里的用例都在锁这道算术。
"""

from __future__ import annotations

from app.style_render import XFADE, _xfade_lines


def _plan(spans: list[float], extras: list[float]) -> list[dict]:
    items, cursor = [], 0.0
    for span, extra in zip(spans, extras):
        items.append({
            "start": round(cursor, 2),
            "end": round(cursor + span, 2),
            "clip": (0.0, span),
            "slow": 1.0,
            "extra": extra,
            "render_span": round(span + extra, 2),
        })
        cursor += span
    return items


def test_xfade_chain_offsets_sit_on_window_ends():
    """叠化必须从上一段的窗口末开始：早了吃掉内容，晚了总长变短。"""
    plan = _plan([3.0, 3.5, 4.0, 2.8], [XFADE, XFADE, XFADE, 0.0])
    lines = _xfade_lines(plan)
    assert len(lines) == 3
    assert lines[0].startswith("[v0][v1]xfade=transition=fade")
    assert "offset=3.00" in lines[0]        # 第一窗口末
    assert "offset=6.50" in lines[1]        # 前两窗累计
    assert "offset=10.50" in lines[2]       # 前三窗累计
    assert lines[-1].endswith("[base]")     # 最后一行的输出是 [base]


def test_fade_never_exceeds_reserved_extra_or_next_span():
    """叠化时长不能超过前段备的余量（会提前取尽第一路），也不能超过下一段自身长度。"""
    # 中间某段只备了 0.1 秒余量：跟它的交界最多溶 0.1 秒
    plan = _plan([3.0, 2.0, 3.0], [XFADE, 0.1, 0.0])
    lines = _xfade_lines(plan)
    assert "duration=0.20" in lines[0]
    assert "duration=0.10" in lines[1]
    # 末段很短时，进末段的叠化也不能比末段还长
    plan = _plan([3.0, 0.4], [XFADE, 0.0])
    lines = _xfade_lines(plan)
    assert "duration=0.20" in lines[0]      # min(0.20, 0.4)


def test_total_length_is_exactly_windows_plus_hold():
    """叠化吃掉的都是预存余量：Σ渲染时长 − Σ叠化 = Σ窗口，一分不差。"""
    spans = [3.0, 3.5, 4.0, 2.8, 3.2]
    extras = [XFADE, XFADE, XFADE, XFADE, 0.0]
    plan = _plan(spans, extras)
    total = sum(item["render_span"] for item in plan)
    for line in _xfade_lines(plan):
        total -= float(line.split("duration=")[1].split(":")[0])
    assert abs(total - sum(spans)) < 1e-6
