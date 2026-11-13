"""配音层：能上豆包就上，上不了必须还能出片。

这块最容易埋的坑是「配了凭据但调用失败」——不能因为这半分钟的接口挂了，
让整条成片出不来。所以每个用例都在验同一条线：
火山能走则走，走不通就悄悄退回 edge-tts，且退回不能重复花钱合成。
"""

from __future__ import annotations

import pytest

from app import tts
from app.db import init_db, transaction


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'tts_%'")
    tts.clear_cache()
    yield
    with transaction() as connection:
        connection.execute("DELETE FROM settings WHERE key LIKE 'tts_%'")
    tts.clear_cache()


def _settings(**values: str) -> None:
    with transaction() as connection:
        for key, value in values.items():
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def _fake_edge(calls: list) -> callable:
    def run(text: str, out_path, voice: str, rate: str) -> None:
        calls.append((text, voice, rate))
        out_path.write_bytes(b"ID3fake-audio" * 40)
    return run


def test_speed_ratio_maps_edge_percent_to_volcano_ratio():
    assert tts._speed_ratio("+10%") == pytest.approx(1.1)
    assert tts._speed_ratio("-5%") == pytest.approx(0.95)
    assert tts._speed_ratio("") == pytest.approx(1.0)
    # 越界要被夹住，别把 3 倍速发给服务端
    assert tts._speed_ratio("+300%") == pytest.approx(2.0)


def test_without_credentials_status_falls_back_to_edge():
    state = tts.status()
    assert state["provider"] == "edge"
    assert state["ready"] is True
    assert "免费" in state["detail"]


def test_synthesize_uses_edge_and_caches(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(tts, "_synth_edge", _fake_edge(calls))
    first = tts.synthesize("230平的办公室。", tmp_path / "a.mp3")
    second = tts.synthesize("230平的办公室。", tmp_path / "b.mp3")
    assert first["provider"] == "edge"
    assert first["cached"] is False
    # 第二次命中缓存：不再调合成，文件照样产出
    assert second["cached"] is True
    assert len(calls) == 1
    assert (tmp_path / "b.mp3").stat().st_size > 0


def test_volcano_used_when_credentials_present(tmp_path, monkeypatch):
    _settings(tts_appid="app", tts_token="token", tts_voice_female="zh_female_test_bigtts")
    used: list = []

    def fake_volcano(text, out_path, voice, rate, conf):
        used.append(voice)
        out_path.write_bytes(b"ID3volcano" * 40)
        return {}

    monkeypatch.setattr(tts, "_synth_volcano", fake_volcano)
    monkeypatch.setattr(tts, "_synth_edge", _fake_edge([]))
    result = tts.synthesize("整层独用，采光通透。", tmp_path / "v.mp3")
    assert result["provider"] == "volcano"
    assert used == ["zh_female_test_bigtts"]


def test_volcano_failure_falls_back_to_edge(tmp_path, monkeypatch):
    _settings(tts_appid="app", tts_token="token", tts_voice_female="zh_female_test_bigtts")
    calls: list = []

    def boom(text, out_path, voice, rate, conf):
        raise tts.TTSError("服务端 500")

    monkeypatch.setattr(tts, "_synth_volcano", boom)
    monkeypatch.setattr(tts, "_synth_edge", _fake_edge(calls))
    result = tts.synthesize("租金另算。", tmp_path / "x.mp3")
    # auto 模式下不能抛错，要静默换免费方案把片子出完
    assert result["provider"] == "edge"
    assert len(calls) == 1


def test_explicit_volcano_raises_instead_of_silently_downgrading(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "_synth_edge", _fake_edge([]))
    with pytest.raises(tts.TTSError):
        tts.synthesize("测试。", tmp_path / "y.mp3", provider="volcano")


def test_missing_voice_id_falls_back_even_with_credentials(tmp_path, monkeypatch):
    _settings(tts_appid="app", tts_token="token")  # 有凭据但没有音色 id
    calls: list = []
    monkeypatch.setattr(tts, "_synth_edge", _fake_edge(calls))
    result = tts.synthesize("采光通透。", tmp_path / "z.mp3")
    assert result["provider"] == "edge"
    assert calls


def test_female_voice_is_not_mistaken_for_male():
    """female 里也有 male 四个字母，按子串判断会把女声配成男声。"""
    conf = {"voice_female": "female-id", "voice_male": "male-id"}
    assert tts.voice_for("female_warm", conf) == "female-id"
    assert tts.voice_for("female_professional", conf) == "female-id"
    assert tts.voice_for("male_professional", conf) == "male-id"


def test_room_number_is_read_digit_by_digit():
    """房号要逐位念：903 → 九零三。

    回归用：稿子里是「位置在D区D19，903。」，原样送进引擎会被念成
    「九百零三」，楼栋还会掉进英文念「D nineteen」。房号念错，
    观众根本找不到那间房，而这两处每套房子都会出现。
    """
    assert tts.speakable("位置在D区D19，903。") == "位置在D区D一九，九零三。"


def test_room_number_with_dash():
    """表里有「202-2」这种门牌，念成「二零二之二」，不能念成「减二」。"""
    assert tts.speakable("202-2也在") == "二零二之二也在"


def test_letter_code_is_spelled_one_by_one():
    assert tts.speakable("A区A01，102。") == "A区A零一，一零二。"


def test_area_price_and_seats_stay_numeric():
    """面积、价格、工位数**不能**逐位念。

    「800平」念成「八零零平」是笑话，比念错房号还尴尬。
    这条守的是 speakable 不打过头：只要后面跟单位、或者前面是金额词，就别动。
    """
    for text in (
        "今天这套800平，先别急着划走",
        "1864平，整层量级",
        "一个月2600，摊到每天87元",
        "这一层能摆下100个工位",
        "月租6500/月",
        "2.6元/㎡/天",
        "隔了3个房间",
        "3号线上盖",
    ):
        assert tts.speakable(text) == text


def test_speakable_does_not_run_twice():
    """改写要幂等：同一条文本重复过一遍不能越改越乱。"""
    once = tts.speakable("D19，903，202-2。")
    assert tts.speakable(once) == once


def test_synthesize_sends_spoken_text_to_engine(tmp_path, monkeypatch):
    """合成时送出去的是改写后的文本，字幕用的仍是原文。

    这条是防回归的关键：如果哪次重构把 speakable 漏掉，
    房号又会变回「九百零三」，而音频听起来"好像也对"，很难发现。
    """
    sent: list[str] = []

    def fake_edge(text, out_path, voice, rate):
        sent.append(text)
        out_path.write_bytes(b"ID3fake")

    monkeypatch.setattr(tts, "_synth_edge", fake_edge)
    monkeypatch.setattr(tts, "_read_cache", lambda key: None)
    monkeypatch.setattr(tts, "_write_cache", lambda *a, **k: None)
    monkeypatch.setattr(tts, "probe_duration", lambda path: 1.0)
    monkeypatch.setenv("TTS_PROVIDER", "edge")

    tts.synthesize("位置在D区D19，903。", tmp_path / "a.mp3", provider="edge")
    assert sent == ["位置在D区D一九，九零三。"]


def test_room_number_is_read_digit_by_digit():
    """房号要逐位念：903 → 九零三。

    回归用：稿子里是「位置在D区D19，903。」，原样送进引擎会被念成
    「九百零三」，楼栋还会掉进英文念「D nineteen」。房号念错，
    观众根本找不到那间房，而这两处每套房子都会出现。
    """
    assert tts.speakable("位置在D区D19，903。") == "位置在D区D一九，九零三。"


def test_room_number_with_dash():
    """表里有「202-2」这种门牌，念成「二零二之二」，不能念成「减二」。"""
    assert tts.speakable("202-2也在") == "二零二之二也在"


def test_letter_code_is_spelled_one_by_one():
    assert tts.speakable("A区A01，102。") == "A区A零一，一零二。"


def test_area_price_and_seats_stay_numeric():
    """面积、价格、工位数**不能**逐位念。

    「800平」念成「八零零平」是笑话，比念错房号还尴尬。
    这条守的是 speakable 不打过头：只要后面跟单位、或者前面是金额词，就别动。
    """
    for text in (
        "今天这套800平，先别急着划走",
        "1864平，整层量级",
        "一个月2600，摊到每天87元",
        "这一层能摆下100个工位",
        "月租6500/月",
        "2.6元/㎡/天",
        "隔了3个房间",
        "3号线上盖",
    ):
        assert tts.speakable(text) == text


def test_speakable_does_not_run_twice():
    """改写要幂等：同一条文本重复过一遍不能越改越乱。"""
    once = tts.speakable("D19，903，202-2。")
    assert tts.speakable(once) == once


def test_synthesize_sends_spoken_text_to_engine(tmp_path, monkeypatch):
    """合成时送出去的是改写后的文本，字幕用的仍是原文。

    这条是防回归的关键：如果哪次重构把 speakable 漏掉，
    房号又会变回「九百零三」，而音频听起来"好像也对"，很难发现。
    """
    sent: list[str] = []

    def fake_edge(text, out_path, voice, rate):
        sent.append(text)
        out_path.write_bytes(b"ID3fake")

    monkeypatch.setattr(tts, "_synth_edge", fake_edge)
    monkeypatch.setattr(tts, "_read_cache", lambda key: None)
    monkeypatch.setattr(tts, "_write_cache", lambda *a, **k: None)
    monkeypatch.setattr(tts, "probe_duration", lambda path: 1.0)
    monkeypatch.setenv("TTS_PROVIDER", "edge")

    tts.synthesize("位置在D区D19，903。", tmp_path / "a.mp3", provider="edge")
    assert sent == ["位置在D区D一九，九零三。"]


def test_price_splits_cut_before_money():
    """价格数字前切开，垫静音由调用方拼回去。

    「一个月2600，摊到每天87元」连着念会糊成一串，观众抓不住价格。
    切点必须落在「月租/一个月」这类引导词之后、数字之前。
    """
    assert tts.price_splits("一个月2600，摊到每天87元。") == [
        "一个月", "2600，摊到每天", "87元。",
    ]
    assert tts.price_splits("月租6500/月，人均两千六。") == [
        "月租", "6500/月，人均两千六。",
    ]


def test_price_splits_ignores_non_money_numbers():
    """只认钱：面积、工位数、隔间、地铁线、房号一律不切。

    满句都是停顿听着像卡带，这条守的是 price_splits 不打过头。
    """
    for text in (
        "今天这套800平，先别急着划走",
        "这一层能摆下100个工位，过道还宽裕",
        "隔了3个房间，老板间、会议室、财务都能分开",
        "位置在D区D一九，九零三",
        "3号线上盖",
    ):
        assert tts.price_splits(text) == [text]


def test_price_splits_skips_sentence_start_and_punctuation():
    """句首的数字垫不了（停顿归上一句管）；标点前不用垫，引擎本来就会停。"""
    assert tts.price_splits("2.6元/㎡/天，含物业。") == ["2.6元/㎡/天，含物业。"]
    assert tts.price_splits("摊到每天，87元。") == ["摊到每天，87元。"]


def test_price_splits_caps_at_two_cuts():
    """一句最多两刀：价格多半在句尾，切出三段就是切错了。"""
    text = "月租6500/月，押金10000，物业费2.6元/㎡/天。"
    assert len(tts.price_splits(text)) <= 3


def test_price_splits_cut_before_money():
    """价格数字前切开，垫静音由调用方拼回去。

    「一个月2600，摊到每天87元」连着念会糊成一串，观众抓不住价格。
    切点必须落在「月租/一个月」这类引导词之后、数字之前。
    """
    assert tts.price_splits("一个月2600，摊到每天87元。") == [
        "一个月", "2600，摊到每天", "87元。",
    ]
    assert tts.price_splits("月租6500/月，人均两千六。") == [
        "月租", "6500/月，人均两千六。",
    ]


def test_price_splits_ignores_non_money_numbers():
    """只认钱：面积、工位数、隔间、地铁线、房号一律不切。

    满句都是停顿听着像卡带，这条守的是 price_splits 不打过头。
    """
    for text in (
        "今天这套800平，先别急着划走",
        "这一层能摆下100个工位，过道还宽裕",
        "隔了3个房间，老板间、会议室、财务都能分开",
        "位置在D区D一九，九零三",
        "3号线上盖",
    ):
        assert tts.price_splits(text) == [text]


def test_price_splits_skips_sentence_start_and_punctuation():
    """句首的数字垫不了（停顿归上一句管）；标点前不用垫，引擎本来就会停。"""
    assert tts.price_splits("2.6元/㎡/天，含物业。") == ["2.6元/㎡/天，含物业。"]
    assert tts.price_splits("摊到每天，87元。") == ["摊到每天，87元。"]


def test_price_splits_caps_at_two_cuts():
    """一句最多两刀：价格多半在句尾，切出三段就是切错了。"""
    text = "月租6500/月，押金10000，物业费2.6元/㎡/天。"
    assert len(tts.price_splits(text)) <= 3


# ------------------------------------------------------------------ v3 接口

class _FakeChunkedResponse:
    """假装 httpx 的流式响应：逐行吐 JSON，data 是 base64 音频段。"""

    def __init__(self, lines: list[str]):
        self._lines = lines
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        yield from self._lines

    def read(self):
        return b""


def _fake_stream(lines: list[str]):
    """把 httpx.Client 换成只吐给定行的假货。"""
    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            return _FakeChunkedResponse(lines)

    return _Client()


def test_v3_chunked_response_is_concatenated(tmp_path, monkeypatch):
    """音频分段返回，拼起来才是完整一句话；SSE 的 data: 前缀也要能吃。"""
    import base64

    part1, part2 = base64.b64encode(b"AAAABBBB"), base64.b64encode(b"CCCC")
    lines = [
        f'{{"code": 0, "data": "{part1.decode()}", "finish": false}}',
        f'data: {{"code": 0, "data": "{part2.decode()}", "finish": true}}',
    ]
    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _fake_stream(lines))
    conf = {**tts.config(), "apikey": "test-key", "emotion": ""}
    out = tmp_path / "v.mp3"
    tts._synth_volcano_v3("念一句。", out, "zh_female_xiaohe_uranus_bigtts", "+10%", conf)
    assert out.read_bytes() == b"AAAABBBBCCCC"


def test_v3_error_code_raises_instead_of_writing_silence(tmp_path, monkeypatch):
    """服务端报错（比如音色 ID 不对）必须抛出来，不能把空音频当成功。"""
    lines = ['{"code": 55000000, "message": "resource ID is mismatched with speaker related resource"}']
    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _fake_stream(lines))
    conf = {**tts.config(), "apikey": "test-key", "emotion": ""}
    with pytest.raises(tts.TTSError, match="55000000"):
        tts._synth_volcano_v3("念一句。", tmp_path / "v.mp3", "bad_voice", "+0%", conf)


def test_v3_speech_rate_clamps_into_api_range(tmp_path, monkeypatch):
    """v3 语速是 [-50, 100] 的整数：edge 的 +10% → 10，+300% 要被夹到 100。"""
    import base64

    captured = {}

    class _Resp(_FakeChunkedResponse):
        pass

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            captured["payload"] = json
            part = base64.b64encode(b"audio").decode()
            return _FakeChunkedResponse([f'{{"code": 0, "data": "{part}", "finish": true}}'])

    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _Client())
    conf = {**tts.config(), "apikey": "test-key", "emotion": ""}
    tts._synth_volcano_v3("x", tmp_path / "a.mp3", "v", "+10%", conf)
    assert captured["payload"]["req_params"]["audio_params"]["speech_rate"] == 10
    tts._synth_volcano_v3("x", tmp_path / "b.mp3", "v", "+300%", conf)
    assert captured["payload"]["req_params"]["audio_params"]["speech_rate"] == 100


def test_v3_emotion_becomes_voice_instruction(tmp_path, monkeypatch):
    """v3 没有 emotion 参数：设置里的情感翻成语音指令 context_texts。"""
    import base64

    captured = {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            captured["payload"] = json
            part = base64.b64encode(b"audio").decode()
            return _FakeChunkedResponse([f'{{"code": 0, "data": "{part}", "finish": true}}'])

    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _Client())
    conf = {**tts.config(), "apikey": "test-key", "emotion": "happy"}
    tts._synth_volcano_v3("x", tmp_path / "v.mp3", "v", "+0%", conf)
    assert captured["payload"]["req_params"]["context_texts"] == [tts._EMOTION_PROMPTS["happy"]]


def test_apikey_alone_marks_ready():
    """新版控制台只发一个 API Key：有 Key 就算可用，不再等 appid+token。"""
    _settings(tts_apikey="a-key", tts_voice_female="zh_female_xiaohe_uranus_bigtts")
    conf = tts.config()
    assert conf["ready"] is True
    assert conf["apikey"] == "a-key"


# ------------------------------------------------------------------ v3 接口

class _FakeChunkedResponse:
    """假装 httpx 的流式响应：逐行吐 JSON，data 是 base64 音频段。"""

    def __init__(self, lines: list[str]):
        self._lines = lines
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        yield from self._lines

    def read(self):
        return b""


def _fake_stream(lines: list[str]):
    """把 httpx.Client 换成只吐给定行的假货。"""
    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            return _FakeChunkedResponse(lines)

    return _Client()


def test_v3_chunked_response_is_concatenated(tmp_path, monkeypatch):
    """音频分段返回，拼起来才是完整一句话；SSE 的 data: 前缀也要能吃。"""
    import base64

    part1, part2 = base64.b64encode(b"AAAABBBB"), base64.b64encode(b"CCCC")
    lines = [
        f'{{"code": 0, "data": "{part1.decode()}", "finish": false}}',
        f'data: {{"code": 0, "data": "{part2.decode()}", "finish": true}}',
    ]
    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _fake_stream(lines))
    conf = {**tts.config(), "apikey": "test-key", "emotion": ""}
    out = tmp_path / "v.mp3"
    tts._synth_volcano_v3("念一句。", out, "zh_female_xiaohe_uranus_bigtts", "+10%", conf)
    assert out.read_bytes() == b"AAAABBBBCCCC"


def test_v3_error_code_raises_instead_of_writing_silence(tmp_path, monkeypatch):
    """服务端报错（比如音色 ID 不对）必须抛出来，不能把空音频当成功。"""
    lines = ['{"code": 55000000, "message": "resource ID is mismatched with speaker related resource"}']
    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _fake_stream(lines))
    conf = {**tts.config(), "apikey": "test-key", "emotion": ""}
    with pytest.raises(tts.TTSError, match="55000000"):
        tts._synth_volcano_v3("念一句。", tmp_path / "v.mp3", "bad_voice", "+0%", conf)


def test_v3_speech_rate_clamps_into_api_range(tmp_path, monkeypatch):
    """v3 语速是 [-50, 100] 的整数：edge 的 +10% → 10，+300% 要被夹到 100。"""
    import base64

    captured = {}

    class _Resp(_FakeChunkedResponse):
        pass

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            captured["payload"] = json
            part = base64.b64encode(b"audio").decode()
            return _FakeChunkedResponse([f'{{"code": 0, "data": "{part}", "finish": true}}'])

    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _Client())
    conf = {**tts.config(), "apikey": "test-key", "emotion": ""}
    tts._synth_volcano_v3("x", tmp_path / "a.mp3", "v", "+10%", conf)
    assert captured["payload"]["req_params"]["audio_params"]["speech_rate"] == 10
    tts._synth_volcano_v3("x", tmp_path / "b.mp3", "v", "+300%", conf)
    assert captured["payload"]["req_params"]["audio_params"]["speech_rate"] == 100


def test_v3_emotion_becomes_voice_instruction(tmp_path, monkeypatch):
    """v3 没有 emotion 参数：设置里的情感翻成语音指令 context_texts。"""
    import base64

    captured = {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json=None, headers=None):
            captured["payload"] = json
            part = base64.b64encode(b"audio").decode()
            return _FakeChunkedResponse([f'{{"code": 0, "data": "{part}", "finish": true}}'])

    monkeypatch.setattr(tts.httpx, "Client", lambda **kw: _Client())
    conf = {**tts.config(), "apikey": "test-key", "emotion": "happy"}
    tts._synth_volcano_v3("x", tmp_path / "v.mp3", "v", "+0%", conf)
    assert captured["payload"]["req_params"]["context_texts"] == [tts._EMOTION_PROMPTS["happy"]]


def test_apikey_alone_marks_ready():
    """新版控制台只发一个 API Key：有 Key 就算可用，不再等 appid+token。"""
    _settings(tts_apikey="a-key", tts_voice_female="zh_female_xiaohe_uranus_bigtts")
    conf = tts.config()
    assert conf["ready"] is True
    assert conf["apikey"] == "a-key"
