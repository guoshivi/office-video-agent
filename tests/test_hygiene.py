"""措辞卫生测试：内部提醒不能在配音里被念出来。

这条规则的现实来源是一条已经出好的片子，口播里念了
「租金和物业费口径请以最新房源确认为准」——等于对客户说我们的价格不确定。
"""

from app.hygiene import clean_internal_sentences, find_internal_phrases, split_sentences


def test_drops_only_the_offending_sentence():
    script = (
        "正在招办公的租客，今天看一套位于F区的房源，面积120平。"
        "租金和物业费口径请以最新房源确认为准。"
        "欢迎私信咨询看房安排。"
    )
    cleaned, removed = clean_internal_sentences(script)
    assert "口径" not in cleaned
    assert "面积120平" in cleaned
    assert "欢迎私信咨询看房安排" in cleaned
    assert removed == ["租金和物业费口径请以最新房源确认为准。"]


def test_clean_text_is_returned_untouched():
    script = "这套一百二十平，深港大厦，朝南。"
    cleaned, removed = clean_internal_sentences(script)
    assert cleaned == script
    assert removed == []


def test_all_internal_falls_back_to_original():
    """整段都是内部用语时原样返回。

    宁可让工作台出一条有瑕疵的片子（审核台还会拦），
    也不能把空文案送进配音——那边会直接报错，中介白等一次。
    """
    script = "备注：口径待确认。"
    cleaned, removed = clean_internal_sentences(script)
    assert cleaned == script
    assert removed == []


def test_multiple_sentences_are_all_removed():
    script = "这套一百二十平。以实际为准。仅供参考，不对外。朝南采光好。"
    cleaned, removed = clean_internal_sentences(script)
    assert cleaned == "这套一百二十平。朝南采光好。"
    assert len(removed) == 2


def test_newlines_and_empty_input():
    assert clean_internal_sentences("") == ("", [])
    assert clean_internal_sentences("   ") == ("", [])
    cleaned, removed = clean_internal_sentences("第一句。\n\n备注：内部用。\n第二句。")
    assert "备注" not in cleaned
    assert "第一句" in cleaned and "第二句" in cleaned
    assert len(removed) == 1


def test_find_internal_phrases_dedupes_in_order():
    assert find_internal_phrases("口径和内部口径，还有备注") == ["口径", "内部", "备注"]
    assert find_internal_phrases("干净的一句话") == []


def test_split_sentences_keeps_punctuation():
    assert split_sentences("一句。两句！三句？") == ["一句。", "两句！", "三句？"]
