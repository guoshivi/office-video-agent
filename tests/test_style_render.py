"""智能选镜与文案生成的基本盘：不出穿帮镜头、文案不重复。"""

from app.copywriting import _dedupe, _fill, fallback_storyboard
from app.style_render import _wrap, render_storyboard  # noqa: F401


# ------------------------------------------------------------------ 文案

SPEC = {
    "shots": [
        {"index": 1, "role": "hook", "sample": "今天带大家实地看房", "seconds": 3},
        {"index": 2, "role": "面积", "sample": "220平3加1格局", "seconds": 4},
        {"index": 3, "role": "亮点", "sample": "入户员工区域", "labels": ["工区"], "seconds": 2},
        {"index": 4, "role": "亮点", "sample": "采光通透", "labels": ["工区", "可容纳20人办公"], "seconds": 2},
        {"index": 5, "role": "亮点", "sample": "带全套公桌公椅", "seconds": 3},
        {"index": 6, "role": "亮点", "sample": "来到老板间", "labels": ["老板办公室"], "seconds": 2.5},
        {"index": 7, "role": "亮点", "sample": "双面采光", "seconds": 2},
        {"index": 8, "role": "亮点", "sample": "沙发一应俱全", "seconds": 2},
        {"index": 9, "role": "亮点", "sample": "高入住即用即租", "seconds": 2},
        {"index": 10, "role": "cta", "sample": "感兴趣的老板赶紧过来看看吧", "seconds": 4},
    ],
    "constraints": {"max_words_per_shot": 20},
}

FACTS = {"area": "220", "partition": "3加1格局", "rent": "88元/平/月"}


def test_fallback_no_duplicates():
    board = fallback_storyboard(SPEC, FACTS)
    texts = [entry["text"] for entry in board]
    assert len(texts) == len(set(texts)), f"规则文案出现重复句：{texts}"
    assert len(board) == len(SPEC["shots"])
    # 面积钩子只出现一次
    assert sum("220" in text for text in texts) <= 2


def test_fallback_rent_line_when_rent_given():
    board = fallback_storyboard(SPEC, FACTS)
    assert any("88" in entry["text"] for entry in board)


def test_fallback_no_rent_placeholder():
    board = fallback_storyboard(SPEC, {"area": "220"})
    for entry in board:
        assert "合适的价格" not in entry["text"]


def test_labels_carry_through():
    board = fallback_storyboard(SPEC, FACTS)
    labeled = [entry for entry in board if "label" in entry]
    assert labeled and labeled[0]["label"]["title"] == "工区"


def test_fill_formats_area_and_partition():
    line = _fill("{area}平，{partition}的办公室", {"面积": "220平", "格局": "3加1"})
    assert line == "220平，3加1的办公室"


def test_dedupe_strips_and_collapses():
    board = [
        {"text": "今天带大家看一套220平的办公室"},
        {"text": "今天带大家看一套220平的办公室。"},   # 仅标点不同
        {"text": "今天带大家看一套220平，采光好"},      # 前 8 字相同
        {"text": "感兴趣的老板赶紧过来看看吧"},
    ]
    out = _dedupe(board)
    assert [entry["text"] for entry in out] == [board[0]["text"], board[3]["text"]]


# ------------------------------------------------------------------ 字幕折行

def test_wrap_breaks_at_punctuation():
    wrapped = _wrap("今天大家实地看房，推荐一套")
    assert wrapped.splitlines()[0] == "今天大家实地看房"


def test_wrap_short_text_single_line():
    assert _wrap("采光通透") == "采光通透"


def test_wrap_long_text_no_orphan():
    wrapped = _wrap("整整一面落地窗白天不用开灯采光通透")
    lines = wrapped.splitlines()
    assert all(len(line) >= 4 for line in lines), lines


# ------------------------------------------------------------------ 渲染参数

def test_render_rejects_bad_audio_mode(tmp_path):
    import pytest

    from app.style_render import RenderError

    with pytest.raises(RenderError):
        render_storyboard([{"text": "测试"}], "不存在也行.mp4", tmp_path / "out.mp4", audio_mode="环绕声")
