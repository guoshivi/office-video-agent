"""花字 / BGM / 字幕标记 / AI 挑词 的单元测试。

这几块都是"锦上添花"的观感层，但失败方式很讨厌——花字画不出来会
让整条出片报错，BGM 音量没限住会削波。所以单独盯住。
"""

from __future__ import annotations

from app import bgm, flower_text, llm
from app.style_render import _flower_phrase, _flower_windows, build_ass, parse_marks


# ------------------------------------------------------------------ 花字

def test_flower_render_makes_transparent_png():
    image = flower_text.render("230平", "burst_orange")
    assert image.mode == "RGBA"
    assert image.size[0] > 200 and image.size[1] > 200


def test_flower_rejects_punctuation_and_long_text():
    import pytest

    with pytest.raises(flower_text.FlowerError):
        flower_text.render("230平，超值", "burst_orange")
    with pytest.raises(flower_text.FlowerError):
        flower_text.render("这是一个太长的短语", "burst_orange")


def test_flower_styles_all_render():
    # 20 种样式逐个画一遍，避免某个分支少写字体参数
    for style in flower_text.STYLES:
        image = flower_text.render("拎包入驻", style)
        assert image.size[0] > 0 and image.size[1] > 0


def test_fonts_are_shipped():
    assert flower_text.fonts_ready()


# ------------------------------------------------------------------ 花字落点

def _sentences(texts, each=8.0):
    return [
        {"text": text, "start": i * each, "end": i * each + each}
        for i, text in enumerate(texts)
    ]


def test_flower_phrase_prefers_manual_mark():
    sentence = {"text": "这套房源拎包入驻，面积230平", "marks": ["拎包入驻"]}
    assert _flower_phrase(sentence) == "拎包入驻"


def test_flower_phrase_falls_back_to_number():
    assert _flower_phrase({"text": "面积230平，格局3隔"}) == "230平"


def test_flower_windows_preview_before_it_is_spoken():
    """花字要比字幕早出现：同屏重复同一句话是浪费画面。"""
    sentences = _sentences([
        "正在找办公室的朋友",
        "今天看一套吉隆坡的房源",
        "面积230平，格局3隔",
        "欢迎私信咨询",
    ])
    windows = _flower_windows(sentences)
    assert windows, "有卖点数字就该出花字"
    assert len(windows) <= 2, "一条片子最多两处花字"
    first = windows[0]
    assert first["text"] == "230平"
    # 落在说出「230平」那句（第 3 句，16 秒）之前
    assert first["start"] < 16.0


def test_flower_windows_skip_when_no_keyword():
    assert _flower_windows(_sentences(["欢迎私信咨询房源", "谢谢观看"])) == []


# ------------------------------------------------------------------ 字幕标记

def test_parse_marks_strips_brackets():
    clean, marks = parse_marks("这套【拎包入驻】，面积230平")
    assert clean == "这套拎包入驻，面积230平"
    assert marks == ["拎包入驻"]


def test_build_ass_highlights_manual_mark():
    ass = build_ass(
        [{"text": "这套【拎包入驻】，面积230平", "start": 0, "end": 3, "label": {}}],
        (1080, 1920), "Noto Sans SC Black",
    )
    # 方括号不能出现在成片字幕里
    assert "【" not in ass and "】" not in ass
    # 手动标记的词要变金色
    assert r"{\c&H00D7FF&}拎包入驻" in ass


def test_build_ass_uses_highlights_from_ai():
    ass = build_ass(
        [{"text": "紧邻三号线地铁口", "start": 0, "end": 3, "highlights": ["三号线"]}],
        (1080, 1920), "Noto Sans SC Black",
    )
    assert r"{\c&H00D7FF&}三号线" in ass


# ------------------------------------------------------------------ BGM

def test_bgm_library_loaded():
    tracks = bgm.list_tracks()
    assert len(tracks) >= 9
    for track in tracks:
        assert track["license"], "每首曲子都要写许可"


def test_bgm_pick_is_stable_and_mood_aware():
    first = bgm.pick("V20261101-64L1Q")
    second = bgm.pick("V20261101-64L1Q")
    assert first["id"] == second["id"], "同一条任务重渲要选同一首"
    calm = bgm.pick("any", "平静")
    assert calm["mood"] == "平静"


# ------------------------------------------------------------------ AI 挑词

def test_pick_highlights_validates_against_line(monkeypatch):
    monkeypatch.setattr(llm, "config", lambda: {"ready": True})
    # 模型给了幻觉词和超长词，只有真实出现在句中的才保留
    monkeypatch.setattr(
        llm, "chat",
        lambda *a, **k: '{"1":["230平","不存在","这一整句话都不是重点词"],"2":["三号线"]}',
    )
    picks = llm.pick_highlights(["面积230平，格局3隔", "紧邻三号线地铁口"])
    assert picks == [["230平"], ["三号线"]]


def test_pick_highlights_returns_empty_without_key(monkeypatch):
    monkeypatch.setattr(llm, "config", lambda: {"ready": False})
    assert llm.pick_highlights(["面积230平"]) == []
