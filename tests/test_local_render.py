"""本机出片：字幕、时长、缺组件时的说法。

这几件事以前都只在"我这台机器"上成立：
- 依赖没写进 requirements，换台电脑就 `No module named edge_tts`
- 字幕用 drawtext，整句一个颜色，卖点数字挑不出来
- 每句硬卡 10 秒上限，目标 45 秒实际只出 40 秒

所以这里盯的是"换台电脑、换个时长，结论还对不对"。
"""

from __future__ import annotations

import pytest

from app import ready
from app.db import init_db
from app.style_render import build_ass, _highlight


# ---------- 字幕 ----------

def test_highlight_marks_numbers_gold():
    text = _highlight("面积信息为230平，格局为3隔")
    assert r"{\c&H00D7FF&}230平{\c&HFFFFFF&}" in text
    assert r"{\c&H00D7FF&}3隔{\c&HFFFFFF&}" in text


def test_highlight_caps_at_two_so_it_does_not_look_noisy():
    text = _highlight("120平 3隔 4层 5个工位 6年")
    assert text.count(r"{\c&H00D7FF&}") == 2


def test_ass_has_one_dialogue_per_sentence_with_timing():
    sentences = [
        {"text": "第一句", "start": 0.0, "end": 4.5, "label": {}},
        {"text": "第二句 230平", "start": 4.5, "end": 9.0, "label": {}},
    ]
    ass = build_ass(sentences, (1080, 1920), "Noto Sans SC Black")
    assert ass.count("Dialogue:") == 2
    assert "0:00:04.50,0:00:09.00" in ass
    assert "PlayResX: 1080" in ass
    assert r"{\c&H00D7FF&}230平" in ass


def test_ass_label_becomes_second_line_not_a_separate_style_run():
    sentences = [
        {"text": "正文", "start": 0.0, "end": 3.0, "label": {"title": "工区", "sub": "可容纳20人"}},
    ]
    ass = build_ass(sentences, (1080, 1920), "Noto Sans SC Black")
    assert "工区\\N可容纳20人" in ass


def test_ass_escapes_braces_so_text_cannot_inject_tags():
    sentences = [{"text": "面积{随便写}230平", "start": 0.0, "end": 3.0, "label": {}}]
    ass = build_ass(sentences, (1080, 1920), "Noto Sans SC Black")
    body = ass.splitlines()[-1]
    assert "{" not in body.replace(r"{\c&H00D7FF&}", "").replace(r"{\c&HFFFFFF&}", "")


def test_long_sentence_becomes_single_line_captions_on_one_timeline():
    """一句话不再折两行：切成短段，每段一行，按语音时间轴接力出现。"""
    sentences = [{
        "text": "面积信息为230平，格局为3隔，可容纳20人办公",
        "start": 0.0, "end": 9.0, "spoken": 7.5, "label": {},
    }]
    ass = build_ass(sentences, (1080, 1920), "Noto Sans SC Black")
    captions = [line for line in ass.splitlines() if line.startswith("Dialogue:") and ",Cap," in line]
    assert len(captions) == 3
    # 一条字幕里不许再出现换行符，屏幕上永远只有一行
    assert all("\\N" not in line for line in captions)
    assert captions[0].endswith("面积信息为{\\c&H00D7FF&}230平{\\c&HFFFFFF&}")

    times = [(line.split(",")[1], line.split(",")[2]) for line in captions]
    assert times[0][0] == "0:00:00.00"
    # 末段贴着语音结束（spoken 7.5 + 0.45 接住句间空隙），而不是跟着被拉长的画面拖到 9 秒
    assert times[-1][1] == "0:00:07.95"
    for previous, following in zip(times, times[1:]):
        assert previous[1] == following[0]


def test_caption_segments_never_exceed_one_line():
    from app.style_render import CAPTION_MAX_CHARS, caption_segments

    for text in (
        "整整一面落地窗白天不用开灯采光通透",
        "这是一套非常罕见的一整层独用办公室，带独立茶水间和露台",
        "租金3.5元/平/天，物业费另计",
    ):
        segments = caption_segments(text)
        assert max(len(piece) for piece, _ in segments) <= CAPTION_MAX_CHARS


def test_decimal_price_is_not_cut_at_the_dot():
    from app.style_render import caption_segments

    pieces = [piece for piece, _ in caption_segments("租金3.5元/平/天，物业费另计")]
    assert pieces[0] == "租金3.5元/平/天"


# ---------- 体检说人话 ----------

@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    # _render_item 会读 ffmpeg_path 设置，所以库得先建好
    init_db()
    monkeypatch.setattr(ready, "_CACHE", {"at": 0.0, "data": None})


def test_missing_component_says_what_and_offers_one_click(monkeypatch):
    monkeypatch.setattr(ready, "RENDER_MODULES", [("definitely_not_a_module_x", "口播配音")])
    item = ready._render_item({"render_engine": "local"})
    assert item.ok is False
    assert item.action == "install_render_deps"
    assert "口播配音" in item.detail
    assert "No module" not in item.detail     # 不把 Python 报错原样丢给使用者


def test_local_engine_ready_when_everything_present():
    item = ready._render_item({"render_engine": "local"})
    assert item.ok is True
    assert item.action == ""
    assert "本机" in item.detail


def test_workbench_engine_without_url_falls_back_to_local_check():
    """选了工作台却没填地址，不该报"没法出片"——本机还能出。"""
    item = ready._render_item({"render_engine": "workbench", "workbench_url": ""})
    assert item.ok is True
