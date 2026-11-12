"""拆解与复核的纯逻辑测试：不加载模型、不读视频文件。

这里每一条都对应一次实际踩过的坑，改动判据时先看这些断言。
"""

from __future__ import annotations


def _rows(band: list[tuple[float, float, str, float]]) -> list[dict]:
    return [
        {"start": start, "end": end, "text": text, "y": y}
        for start, end, text, y in band
    ]


# ------------------------------------------------------------------ 骨架

def test_skeleton_roles_always_opens_and_closes():
    from app.refvideo import skeleton_roles

    assert skeleton_roles(0) == []
    assert skeleton_roles(1) == ["hook"]
    assert skeleton_roles(2) == ["hook", "cta"]
    roles = skeleton_roles(8)
    assert roles[0] == "hook" and roles[-1] == "cta"
    # 中间不能出现 hook/cta，否则一条片子会喊两次开头、收两次尾
    assert "hook" not in roles[1:-1] and "cta" not in roles[1:-1]


def test_shots_from_cuts_without_any_text():
    from app.refvideo import _shots_from_cuts

    shots = _shots_from_cuts(25.4, [3.3, 8.0, 11.1, 14.2, 17.4, 20.5, 24.0])
    assert len(shots) == 8
    assert shots[0]["start"] == 0.0
    assert abs(shots[-1]["end"] - 25.4) < 0.01
    assert all(shot["sample"] == "" for shot in shots)


def test_shots_from_cuts_merges_flicker():
    """0.2 秒内连切两个点，是同一帧被判定两次，不能当两个镜头。"""
    from app.refvideo import _shots_from_cuts

    shots = _shots_from_cuts(12.0, [4.0, 4.15, 4.3, 8.0])
    assert len(shots) == 3


def test_build_spec_without_text_does_not_raise():
    from app.refvideo import build_spec

    spec = build_spec(25.4, [3.3, 8.0, 11.1], [], [])
    assert spec["text_source"] == "none"
    assert len(spec["shots"]) == 4
    assert "没读到台词" in " ".join(spec["tips"])


def test_build_spec_rejects_zero_duration():
    import pytest

    from app.refvideo import RefVideoError, build_spec

    with pytest.raises(RefVideoError):
        build_spec(0, [], [], [])


# ------------------------------------------------------------------ 字幕去噪

def test_subtitle_band_skips_packaging_text():
    """包装上的字只在某个镜头出现；字幕铺满全片。靠时间连续性区分。"""
    from app.refvideo import _pick_subtitle_band

    subtitles = _rows([
        # 真字幕：每 0.8 秒换一句，从 0 铺到 12 秒
        (0.0, 0.8, "网上假货太多了", 0.81),
        (0.8, 1.6, "认准红色包装", 0.81),
        (1.6, 2.4, "正面有防伪码", 0.81),
        (3.2, 4.0, "背面条码471开头", 0.81),
        (4.0, 4.8, "产地台湾", 0.81),
        (6.0, 6.8, "一撕一贴超简单", 0.81),
        (8.0, 8.8, "特别舒服", 0.81),
        (10.0, 10.8, "50贴到手99块9", 0.81),
        (11.6, 12.4, "点左下角看看", 0.81),
        # 包装上的说明：只在 2.4~3.2 和 8.8~10 出现，中间大段空白
        (2.4, 3.0, "草本植物萃取精油", 0.70),
        (9.0, 9.6, "手足肩颈腰背", 0.70),
    ])
    band = _pick_subtitle_band(subtitles)
    assert band, "应该挑出字幕带"
    assert all(abs(row["y"] - 0.81) < 0.01 for row in band)
    assert "网上假货太多了" in [row["text"] for row in band]


def test_subtitle_band_ignores_upper_third():
    """上半屏是功能区角标，不可能是口播字幕。"""
    from app.refvideo import _pick_subtitle_band

    rows = _rows([(i * 0.8, i * 0.8 + 0.7, f"第{i}句", 0.20) for i in range(8)])
    assert _pick_subtitle_band(rows) == []


def test_subtitle_band_returns_empty_when_nothing_looks_like_captions():
    from app.refvideo import _pick_subtitle_band

    assert _pick_subtitle_band([]) == []
    assert _pick_subtitle_band(_rows([(0.0, 0.5, "孤立一行", 0.81)])) == []


def test_coalesce_merges_same_line_read_differently():
    from app.refvideo import _coalesce

    rows = _rows([
        (0.0, 0.8, "怎么分辨真假呢", 0.81),
        (0.8, 1.6, "怎么分瓣眞假呢", 0.81),   # 同一句的另一种读法
        (1.6, 2.4, "认准红色包装", 0.81),     # 换句了
    ])
    merged = _coalesce(rows)
    assert len(merged) == 2
    assert merged[0]["end"] == 1.6


def test_coalesce_never_collapses_whole_video():
    """早期版本只按 0.9 秒间隙合并，把整条片子并成了两句。"""
    from app.refvideo import _coalesce

    rows = _rows([
        (index * 0.83, index * 0.83 + 0.8, text, 0.81)
        for index, text in enumerate(["网上假货多", "认准包装", "防伪码可查", "条码471", "产地台湾", "特别舒服"])
    ])
    assert len(_coalesce(rows)) == 6


def test_covers_enough_rejects_a_few_stray_lines():
    from app.refvideo import _covers_enough

    assert _covers_enough([], 25.0) is False
    assert _covers_enough([{"start": 10.0, "end": 11.0}], 25.0) is False
    assert _covers_enough([{"start": i * 2.0, "end": i * 2.0 + 1.8} for i in range(8)], 25.0) is True


# ------------------------------------------------------------------ 复核回填

def _make_template(payload: dict) -> str:
    import json

    from app.db import init_db, transaction, utc_now

    init_db()
    with transaction() as connection:
        connection.execute(
            """INSERT INTO style_templates(id,name,source_type,source_path,status,spec_json,
               duration_s,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                payload["id"], payload["name"], "upload", "data/templates/x/source.mp4", "ready",
                json.dumps(payload["spec"], ensure_ascii=False), payload["duration"],
                utc_now(), utc_now(),
            ),
        )
    return payload["id"]


def _spec_with_scenes() -> dict:
    return {
        "engine": "local",
        "text_source": "caption",
        "duration": 20.0,
        "cuts": [5.0, 10.0, 15.0],
        "shots": [
            {"index": 1, "scene": 1, "start": 0.5, "seconds": 4.0, "role": "hook", "sample": "网上假货多"},
            {"index": 2, "scene": 2, "start": 5.5, "seconds": 4.0, "role": "zone", "sample": "本工版通"},
            {"index": 3, "scene": 3, "start": 10.5, "seconds": 4.0, "role": "furn", "sample": "50贴99块9"},
            {"index": 4, "scene": 4, "start": 15.5, "seconds": 3.0, "role": "cta", "sample": "点左下角"},
        ],
    }


def test_apply_review_sets_roles_from_scenes_and_recomputes_structure():
    from app.db import transaction
    from app.reviewpack import apply_review

    template_id = _make_template({"id": "rv1", "name": "样片", "duration": 20.0, "spec": _spec_with_scenes()})
    result = apply_review(
        template_id,
        scenes=[
            {"scene": 1, "subject": "手持产品", "role": "hook", "why": "先立焦虑"},
            {"scene": 2, "subject": "正面特写", "role": "亮点", "why": ""},
            {"scene": 3, "subject": "整堆平铺", "role": "value", "why": ""},
            {"scene": 4, "subject": "箱内特写", "role": "cta", "why": "指路"},
        ],
        summary="先立焦虑再报价",
    )
    shares = {item["role"]: item["share"] for item in result["structure"]}
    # 结构按镜头时长算：亮点/value 各 5 秒、hook/cta 各 5 秒
    assert set(shares) == {"hook", "亮点", "value", "cta"}
    assert abs(sum(shares.values()) - 1.0) < 0.05

    with transaction() as connection:
        import json

        spec = json.loads(
            connection.execute(
                "SELECT spec_json FROM style_templates WHERE id=?", (template_id,)
            ).fetchone()["spec_json"]
        )
    assert spec["summary"] == "先立焦虑再报价"
    assert spec["scenes"][0]["subject"] == "手持产品"


def test_apply_review_keeps_role_of_scene_without_any_line():
    """短的收尾镜常常一句台词都没有，角色不能跟着台词一起消失。"""
    from app.reviewpack import apply_review

    spec = _spec_with_scenes()
    spec["shots"] = spec["shots"][:3]      # 第 4 镜没有任何台词
    template_id = _make_template({"id": "rv2", "name": "样片", "duration": 20.0, "spec": spec})
    result = apply_review(
        template_id,
        scenes=[{"scene": 4, "subject": "箱内特写", "role": "cta"}],
    )
    assert "cta" in [item["role"] for item in result["structure"]]


def test_apply_review_drops_noise_lines():
    from app.reviewpack import apply_review

    template_id = _make_template({"id": "rv3", "name": "样片", "duration": 20.0, "spec": _spec_with_scenes()})
    result = apply_review(
        template_id,
        shots=[{"index": 2, "drop": True}, {"index": 3, "line": "50贴到手99块9"}],
    )
    assert result["dropped"] == 1
    assert result["shots"] == 3


# ------------------------------------------------------------------ 套用前压缩

def test_compact_spec_one_line_per_scene():
    """模板是逐句拆的，套用要按镜头压成一句，否则开头会连说三句钩子。"""
    from app.style_template import _compact_spec

    spec = {
        "scenes": [
            {"scene": 1, "role": "hook"},
            {"scene": 2, "role": "亮点"},
            {"scene": 3, "role": "亮点"},
            {"scene": 4, "role": "value"},
            {"scene": 5, "role": "cta"},
        ],
        "shots": [{"index": i, "role": "hook", "sample": "x"} for i in range(1, 18)],
    }
    compact = _compact_spec(spec)
    assert len(compact["shots"]) == 5
    assert [shot["role"] for shot in compact["shots"]] == ["hook", "zone", "furn", "value", "cta"]


def test_compact_spec_rotates_spot_topics():
    """连着四个卖点镜不能四句都说采光。"""
    from app.style_template import _compact_spec

    spec = {"scenes": [{"scene": 1, "role": "亮点"} for _ in range(4)], "shots": []}
    topics = [shot["role"] for shot in _compact_spec(spec)["shots"]]
    assert topics == ["zone", "furn", "boss", "light"]


def test_compact_spec_without_scenes_falls_back_to_roles():
    from app.style_template import _compact_spec

    spec = {
        "shots": [
            {"index": 1, "role": "hook", "sample": "a"},
            {"index": 2, "role": "hook", "sample": "b"},
            {"index": 3, "role": "cta", "sample": "c"},
        ]
    }
    compact = _compact_spec(spec)
    assert [shot["role"] for shot in compact["shots"]] == ["hook", "cta"]
