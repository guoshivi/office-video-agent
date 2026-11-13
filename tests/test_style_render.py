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
    texts = [entry["text"] for entry in board]
    # 口播念折算后的月租，不念「88元/平/月」这种只有同行听得懂的单价
    assert any("1.9万" in text or "691" in text for text in texts), texts
    assert not any("88元/平" in text for text in texts), texts


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


def test_no_furniture_promise_without_field():
    """办公室租赁一般不带家具：房源没写「带家具」，画面拍到桌椅也不许承诺配齐。

    回归用：句库 furn 那组四句全是"全套配齐 / 拎包办公"，而模板里
    "拍到桌椅"的镜头很常见（role=furn）。两件事一叠加，等于默认在说假话——
    上一个租客还没搬走的桌椅，被讲成了交付配置，客户来看房发现搬空了要吵。
    """
    board = fallback_storyboard(SPEC, {"area": "220", "partition": "3加1格局"})
    text = "".join(entry["text"] for entry in board)
    for word in ("配齐", "全套", "拎包", "家具全送", "一分钱不用花"):
        assert word not in text, text


def test_furniture_never_promised_even_when_field_says_so():
    """这个业务不配家具：连"带家具"的脏数据也不许讲。

    问过：业主从来不配家具，隔断也是做好的现状，没有定制。
    而「家具」这一栏在表里被当杂物栏用——50 条里只有 2 条写着带家具，
    其余填的是电梯口 / 毛坯 / 带露台 / 展厅 / 店面。
    上一版留了"填了「带家具」就会讲"的开关，等于给脏数据留了道后门：
    填错一条就能发出一条假广告，客户来看房桌子搬空了要吵。
    """
    facts = {"area": "220", "partition": "3加1格局", "furnishing_layout": "带全套家具"}
    board = fallback_storyboard(SPEC, facts)
    text = "".join(entry["text"] for entry in board)
    for word in ("配齐", "全套", "拎包", "家具", "桌椅"):
        assert word not in text, text


def test_furn_shot_still_gets_a_line():
    """furn 镜头不能因为句库下线就空着。

    角色名还叫 furn——refvideo 的镜头骨架、前端下拉、style_template 的
    卖点轮换都在用它。句库那组删掉以后必须走别名落到 seat，
    否则这一镜直接没台词，成片会多出一段画面没有旁白。
    """
    from app.copywriting import _topic_for

    assert _topic_for("", "furn") == "seat"
    assert _topic_for("工位实景，打印机和文件", "furn") == "seat"


def test_partition_never_promises_customization():
    """格局只说"现在隔了几间"，不说"可以怎么隔"。

    回归用：1 隔被讲成"一个大开间，中间没切，怎么分你自己定"——
    既说错了几间，又承诺了做不到的事（隔断是业主做好的，没有定制这回事）。
    另外「0隔」和「空着」以前都算 0，导致明确的大开间永远说不出来。
    """
    from app.services import _partition_count, _partition_lines

    assert _partition_count("可定制") is None
    assert _partition_count("") is None
    assert _partition_count("2600平") is None
    assert _partition_count("0隔") == 0
    assert _partition_count("1隔") == 1
    assert _partition_count("13隔") == 13

    for count in (0, 1, 2, 3, 6):
        text = "".join(_partition_lines(count))
        for word in ("自己定", "自己划", "想怎么改", "按你的"):
            assert word not in text, text
    assert "大开间" in _partition_lines(0)[0]
    assert "大开间" not in _partition_lines(1)[0]


def test_desk_phrase_does_not_inflate():
    """工位量级要按 high 落在哪一档报，不能"≤300 就说两百多个"。

    回归用：800 平按 7.5~10.5 平/位是 76~107 个，旧逻辑报成"两百多个"，
    报多一倍——客户一进场就露馅，比不说还糟。
    """
    from app.services import _desk_phrase

    assert _desk_phrase(800) == "近百个"
    assert _desk_phrase(1600) == "两百多个"
    assert _desk_phrase(4000) == "三百个以上"
    # 小面积报区间，且是中文数字（口播念得出来）
    assert _desk_phrase(60) == "六个到八个之间"
