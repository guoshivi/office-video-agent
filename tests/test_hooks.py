"""钩子库与文案引擎：数字要算得准，算不准的宁可不说话。"""

import pytest

from app import hooks
from app.copywriting import _norm_facts, build_titles, generate_storyboard, join_script

FACTS = {"区域": "望京", "面积": "180", "格局": "3+1", "租金": "3.5元/平/天", "家具装修": "全套"}


# ------------------------------------------------------------------ 租金折算

def test_daily_unit_price_to_monthly():
    assert hooks.monthly_rent({"面积": "180", "租金": "3.5元/平/天"}, 180) == pytest.approx(18900)


def test_wan_per_month():
    assert hooks.monthly_rent({"面积": "180", "租金": "2.8万/月"}, 180) == pytest.approx(28000)


def test_sqm_per_month():
    assert hooks.monthly_rent({"面积": "220", "租金": "88元/平/月"}, 220) == pytest.approx(19360)


def test_bare_number_is_unit_price_per_sqm_per_month():
    """房源表 657 条只写「40」这种裸数字，中位 26——只能是元/㎡/月。"""
    assert hooks.monthly_rent({"面积": "180", "租金": "40"}, 180) == pytest.approx(7200)


def test_bare_number_still_reads_daily_when_monthly_is_absurd():
    """只写「3.5」时按单价月算只有 3.5 元/㎡/月，明显不对，应改判成单价天。"""
    assert hooks.monthly_rent({"面积": "180", "租金": "3.5"}, 180) == pytest.approx(18900)


def test_rent_plus_property_fee_is_summed():
    """35+5 是租金 35 加物业 5，客户实付合计 40。"""
    result = hooks.explain_rent({"面积": "1600", "租金": "35+5"}, 1600)
    assert result["monthly"] == pytest.approx(64000)
    assert result["unit"] == "元/㎡/月"


def test_inclusive_price_does_not_add():
    assert hooks.monthly_rent({"面积": "175", "租金": "35含物业"}, 175) == pytest.approx(6125)


def test_total_price_for_single_room_without_area():
    """面积写的是「单间」，那 900 就是一间房整月 900。"""
    assert hooks.monthly_rent({"面积": "单间", "租金": "900包物业"}, None) == pytest.approx(900)


def test_rent_listed_as_year_total_is_rejected():
    """318 万挂在 472 平上折出 6737 元/㎡/月，明显不是月租，不报价。"""
    assert hooks.monthly_rent({"面积": "472", "租金": "318万"}, 472) is None


def test_negotiable_rent_is_not_a_number():
    assert hooks.monthly_rent({"面积": "956", "租金": "面谈"}, 956) is None


def test_context_derives_talkable_numbers():
    ctx = hooks.context(FACTS)
    assert ctx["monthly"] == "1.9万"
    assert ctx["daily"] == "630"
    assert ctx["seats"] == "23"
    assert ctx["per_person"] == "822"


# ------------------------------------------------------------------ 模板渲染

def test_missing_field_skips_template():
    """没租金时不许出现「一天只要元」这种半截话。"""
    thin = {"面积": "180"}
    for item in hooks.candidates(thin, "any"):
        assert "{" not in item["text"]
        assert not item["text"].rstrip("。").endswith("元")


def test_three_tiers_when_facts_complete():
    picked = hooks.pick(FACTS, "any", 3)
    assert len(picked) == 3
    assert len({item["tier"] for item in picked}) == 3


def test_pick_is_stable_per_property():
    first = [item["text"] for item in hooks.pick(FACTS, "any", 3)]
    second = [item["text"] for item in hooks.pick(FACTS, "any", 3)]
    assert first == second


def test_boss_audience_gets_its_own_line():
    boss = [item["text"] for item in hooks.candidates(FACTS, "boss")]
    plain = [item["text"] for item in hooks.candidates(FACTS, "any")]
    assert any("客户来了" in text for text in boss)
    assert not any("客户来了" in text for text in plain)


def test_norm_facts_accepts_chinese_keys():
    """接口层给的是中文 key，老调用方给英文 key，两边都不能丢字段。"""
    norm = _norm_facts({"区域": "望京", "面积": "180", "租金": "3.5元/平/天", "家具装修": "全套"})
    assert norm["区域"] == "望京"
    assert norm["租金"] == "3.5元/平/天"
    assert norm["家具"] == "全套"


# ------------------------------------------------------------------ 成篇

def test_no_template_still_produces_board():
    board, origin = generate_storyboard({}, FACTS, prefer_api=False)
    assert origin == "rules"
    assert len(board) >= 5


def test_cost_line_uses_monthly_not_unit_price():
    """口播念「一天 630」，不念「3.5元/平/天」——老板听不懂单价。"""
    board, _ = generate_storyboard({}, FACTS, prefer_api=False)
    script = join_script(board)
    assert "1.9万" in script or "822" in script
    assert "3.5元/平" not in script


def test_script_punctuation_matches_shots():
    board, _ = generate_storyboard({}, FACTS, prefer_api=False)
    script = join_script(board)
    assert script.endswith("。")
    assert script.count("。") == len(board)


def test_titles_fit_platform_limits():
    titles = build_titles(FACTS)
    assert titles["douyin"] and all(len(text) <= 30 for text in titles["douyin"])
    assert titles["xiaohongshu"] and all(len(text) <= 20 for text in titles["xiaohongshu"])


def test_titles_survive_missing_rent():
    titles = build_titles({"区域": "望京", "面积": "180"})
    assert titles["douyin"], "租金算不出来也得有标题，不能空着"
