"""开场房源卡与结尾邀约卡：内容只能来自房源表，时间只能落在自己的窗口里。

开场卡是钩子的实物证据（面积+房号），结尾卡是转化钩子（带看邀约）。
两卡都走 ASS Card 样式：全屏大字，说错话比不说更显眼——所以用例重点锁
「没事实就不出卡」「时间窗不越界」。
"""

from __future__ import annotations

import re

from app.style_render import build_ass


def _sentences():
    return [
        {"text": "今天这套八百平先别急着划走。", "start": 0.0, "end": 3.2, "spoken": 3.0},
        {"text": "位置在D区D19，903。", "start": 3.2, "end": 6.5, "spoken": 3.1},
    ]


def _build(**kwargs):
    kwargs.setdefault("hook_card", None)
    kwargs.setdefault("cta_card", None)
    return build_ass(_sentences(), (1080, 1920), "Noto Sans SC", **kwargs)


def test_hook_card_slides_in_over_first_sentence_window():
    ass = _build(hook_card={"title": "800平", "sub": "D19 · 903"})
    hook = [ln for ln in ass.splitlines() if ",Card," in ln and "\\move" in ln]
    assert len(hook) == 1
    line = hook[0]
    # 窗口 = 第一句：起 0:00:00.00，止于第一句末
    assert line.startswith("Dialogue: 0,0:00:00.00,0:00:03.20")
    # 从画面外左侧滑到水平居中（an5 + move 终点 x=540）
    assert "\\move(" in line and ",540," in line
    # 淡入淡出都得有，不能硬出现硬消失
    assert "\\fad(120,300)" in line
    # 副标用小号字
    assert "\\fs" in line and "D19 · 903" in line


def test_cta_card_fades_in_at_given_window():
    ass = _build(cta_card={"title": "800平 · D19 903", "sub": "带看随时约 · 评论区留言",
                           "start": 6.1, "end": 8.0})
    cta = [ln for ln in ass.splitlines() if ",Card," in ln and "\\pos" in ln]
    assert len(cta) == 1
    line = cta[0]
    assert line.startswith("Dialogue: 0,0:00:06.10,0:00:08.00")
    # 定格期间挂着，只淡入不淡出
    assert "\\fad(350,0)" in line
    assert "带看随时约" in line


def test_no_cards_no_card_dialogues():
    """没给卡就一条 Card 对话都不出，样式留着不碍事。"""
    ass = _build()
    assert ",Card," not in ass.split("[Events]")[1]
    assert "Style: Card," in ass  # 样式定义无害地待着


def test_cta_card_with_bad_window_is_skipped():
    """start/end 不合法（缺 end 或倒挂）时静默不出卡，不产出炸渲染的 Dialogue。"""
    ass = _build(cta_card={"title": "x", "start": 5.0, "end": 5.0})
    assert "\\pos" not in ass or ",Card," not in ass.split("\\pos")[0]
    assert len([ln for ln in ass.splitlines() if ",Card," in ln]) == 0


# ------------------------------------------------------------------ 逐字 / 底条 / 进度条

from app.style_render import _typewriter, _caption_box_line, _progress_line  # noqa: E402


def test_typewriter_tags_every_visible_char_with_karaoke():
    """每个可见字前面都得有 \\k：标签里的字不算，高亮色标签原样保留。"""
    body = "{\\c&H00D7FF&}800{\\c&HFFFFFF&}平"          # 高亮过的正文
    out = _typewriter(body, 0.0, 2.0)
    assert out.startswith("{\\2a&HFF&\\fscx90\\fscy90\\t(0,180,\\fscx100\\fscy100)}")
    # 「8」「0」「0」「平」四个可见字各带一个 \k；数值随段长摊
    karaoke_count = out.count("{\\k")
    assert karaoke_count == 4
    assert "{\\c&H00D7FF&}" in out                       # 高亮标签没被吃掉
    # \k 单位是百分之一秒：2.0s / 4 字 = 50cs，四字共 200cs = 整段
    assert "{\\k50}" in out


def test_typewriter_never_runs_faster_than_4cs_per_char():
    """极短的段也得保证每个字至少亮 4 百分之一秒，不闪成频闪灯。"""
    out = _typewriter("一二三四五六七八九十一二三四五六七八九十"[:20], 0.0, 0.06)
    assert "{\\k4}" in out


def test_build_ass_pop_off_keeps_whole_sentence():
    ass = _build(pop=False)
    body = [ln for ln in ass.splitlines() if ",Cap," in ln][0]
    assert "{\\k" not in body and "\\t(" not in body


def test_caption_box_sits_below_text_on_negative_layer():
    line = _caption_box_line("今天这套八百平", 0.0, 3.0, 1080, 1920, 120, 83.3, 260, 1.0)
    assert line.startswith("Dialogue: -1,")            # 垫底，不盖字
    assert "\\p1}" in line and "{\\p0}" in line   # 绘图指令成对闭合
    assert "\\1a&H5A&" in line                          # 半透明黑
    assert " b " in line                                # 圆角（贝塞尔）


def test_progress_bar_steps_from_zero_to_full_width():
    """进度条是分段静态矩形（这个 libass 不插值动画裁剪），每步只长一点。"""
    lines = _progress_line(30.0, 1080, 1920, 1.0).splitlines()
    assert len(lines) == 120                      # 30s / 0.25s
    widths = [int(re.search(r"m 0 \d+ l (\d+) ", ln).group(1)) for ln in lines]
    assert widths == sorted(widths)               # 只增不减
    assert widths[0] < 20 and widths[-1] == 1080  # 起步细、收尾满宽
    # 时间片首尾相接：没有空档，进度条不会闪
    prev_end = "0:00:00.00"
    for ln in lines:
        parts = ln.split(",")
        assert parts[1] == prev_end
        prev_end = parts[2]
    assert lines[-1].split(",")[2] == "0:00:30.00"


def test_build_ass_progress_spans_to_cta_end():
    """进度条要走到片尾（含定格），不能在末句就停。"""
    ass = _build(cta_card={"title": "800平", "start": 6.1, "end": 8.2})
    prog = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")]
    assert prog and prog[-1].split(",")[2] == "0:00:08.20"


def test_hook_card_never_outlives_three_seconds():
    """钩子卡是前 3 秒的东西：第一句很长也不能一直挂着。"""
    sentences = [{"text": "这一句特别长能念六秒", "start": 0.0, "end": 6.0, "spoken": 5.8}]
    ass = build_ass(sentences, (1080, 1920), "Noto Sans SC", hook_card={"title": "800平"})
    card = [ln for ln in ass.splitlines() if ",Card," in ln][0]
    assert card.split(",")[2] == "0:00:03.20"


def _cap_style_fields(ass):
    """把 Cap 样式行拆成字段字典，方便断言描边配置。"""
    line = next(ln for ln in ass.splitlines() if ln.startswith("Style: Cap,"))
    names = ("Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
             "OutlineColour", "BackColour", "Bold", "Italic", "Underline", "StrikeOut",
             "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle", "Outline",
             "Shadow", "Alignment", "MarginL", "MarginR", "MarginV", "Encoding")
    return dict(zip(names, line[len("Style: "):].split(",")))


def test_caption_has_no_black_border_when_box_on():
    """底条在时字幕是干净白字：黑描边和阴影都清零（用户嫌那个黑框脏）。"""
    fields = _cap_style_fields(_build(box=True))
    assert fields["Outline"] == "0"
    assert fields["Shadow"] == "0"


def test_caption_border_returns_when_box_off():
    """底条关掉时描边必须回来：亮素材上无描边的白字会看不清。"""
    fields = _cap_style_fields(_build(box=False))
    assert int(fields["Outline"]) >= 3
    assert int(fields["Shadow"]) >= 1
