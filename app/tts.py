"""口播配音：豆包语音合成（火山引擎）为主，edge-tts 兜底。

为什么要有这一层：配音是唯一「换一家服务商就明显更好听」的环节，
把它焊死在 edge-tts 里，以后想换就得改渲染器。所以这里做成 provider，
上层只认 synthesize() 一个函数。

三档来源：
- volcano  豆包语音合成，音色像真人、支持情绪，配了凭据才走
- edge     edge-tts，免费、零依赖、断网也能跑（默认兜底）
- auto     有凭据走 volcano，没有或调用失败自动退 edge（默认）

合成结果按「文本 + 音色 + 语速 + 来源」哈希缓存在 data/cache/tts：
同一句改稿重出片不重复合成，既省字符数也省时间——改一句只重合成一句。

计费口径提醒：火山按提交的字符数计费（SSML、标点、空格都算），
所以缓存不只是提速，也是省钱。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

import httpx

from .db import BASE_DIR, get_settings
from .shots import probe_duration

logger = logging.getLogger(__name__)

CACHE_DIR = BASE_DIR / "data" / "cache" / "tts"


# ------------------------------------------------------------------ 号码读法
#
# 稿子里的「903」是写给人看的，念出来该是「九零三」——交给引擎会念成
# 「九百零三」，「D19」还会掉进英文念「D nineteen」。房号念错，观众找不到房，
# 而这两处每套房子、每条片子都会出现。
#
# 反过来，面积、价格、工位数**不能**逐位念：「800平」念成「八零零平」是笑话。
# 所以只在确认是房号时才动，剩下的一概交给引擎——它能念对。

_CN_DIGIT = "零一二三四五六七八九"

# 数字后面跟这些，说明是数量/面积/价格，按数值念
_NUMERIC_AFTER = ("平", "㎡", "m²", "元", "块", "个", "人", "层", "米", "天", "号", "间", "位", "%")
# 数字前面是这些，说明是金额，按数值念（「一个月2600」不能念「二六零零」）
_NUMERIC_BEFORE = ("月租", "租金", "一个月", "每天", "一天", "押金", "总价", "单价", "约", "共")

_NUMBER_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?:-(\d+))?(?![\d.])")


def _spoken_digits(run: str) -> str:
    """903 → 九零三。房号永远逐位念，不管几位数。"""
    return "".join(_CN_DIGIT[int(ch)] for ch in run)


def speakable(text: str) -> str:
    """把写给人看的号码，改成人念得出来的样子。

    只改两处：
    - 房号（3~4 位纯数字，前后没有单位）：903 → 九零三，202-2 → 二零二之二
    - 楼栋（字母 + 数字）：D19 → D一九，A01 → A零一

    改完的文本只用于**合成**，字幕照旧显示阿拉伯数字——字幕里写「九零三」
    反而看不懂。
    """
    raw = str(text or "")
    if not raw:
        return raw

    def replace(match: re.Match[str]) -> str:
        run, sub = match.group(1), match.group(2)
        if "." in run:                      # 2.6 元/㎡ 这类小数，永远是数值
            return match.group(0)
        after = raw[match.end(): match.end() + 2]
        before = raw[max(0, match.start() - 6): match.start()]
        if after.startswith(_NUMERIC_AFTER) or after.startswith(("/", "／")):
            return match.group(0)
        if any(before.endswith(word) for word in _NUMERIC_BEFORE):
            return match.group(0)
        # 字母打头的编号一律逐位：D19 / A01 / B2 都是门牌，念成"十九"没人听得懂
        if re.search(r"[A-Za-z]\s*$", before):
            return _spoken_digits(run) + (f"之{_spoken_digits(sub)}" if sub else "")
        if len(run) < 3:                    # 一两位的裸数字多半是"2隔""5号线"，别碰
            return match.group(0)
        return _spoken_digits(run) + (f"之{_spoken_digits(sub)}" if sub else "")

    return _NUMBER_RE.sub(replace, raw)


# 价格数字前垫一小段静音：让「月租…两千六」里的数从语流里跳出来，
# 不垫的话一串「月租两千六摊到每天八十七」糊成一句，观众抓不住价格。
# 只认钱——数字后面跟钱单位，或前面是金额引导词。面积（平）、数量（个/间）、
# 地铁（号线）一律不垫：满句都是停顿听着像卡带。
_MONEY_UNITS = ("元", "块", "/月", "/天", "/㎡", "/位", "万", "元/平")
_MONEY_CUES = ("月租", "租金", "物业费", "押金", "一个月", "每月", "人均", "每人", "摊到", "均摊")
_BARE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def price_splits(text: str, max_splits: int = 2) -> list[str]:
    """在金额数字前把句子切开，切点由调用方垫静音再拼回来。

    返回单元素 = 这句不用垫。三条规则把误伤挡在外面：
    - 句首的数字不切：句子开头的停顿归上一句的句间停顿管；
    - 切点前是标点的不切：引擎在标点处本来就会停，再垫就拖沓了；
    - 一句最多切 2 刀，价格多半在句尾，多了就是切错。
    """
    raw = str(text or "").strip()
    if not raw:
        return []

    cuts: list[int] = []
    for match in _BARE_NUMBER_RE.finditer(raw):
        start, end = match.span()
        after = raw[end: end + 3]
        before = raw[max(0, start - 4): start]
        if not (after.startswith(_MONEY_UNITS) or any(before.endswith(cue) for cue in _MONEY_CUES)):
            continue
        if start == 0 or raw[start - 1] in "，。、；：！？,.;:!? ":
            continue
        cuts.append(start)
        if len(cuts) >= max_splits:
            break
    if not cuts:
        return [raw]

    pieces: list[str] = []
    prev = 0
    for cut in cuts:
        pieces.append(raw[prev:cut])
        prev = cut
    pieces.append(raw[prev:])
    return pieces


# 火山的两套鉴权：新版控制台给 API Key（X-Api-Key），老版给 appid + access token。
# 两种都支持，用户拿到哪种填哪种，不用去理解区别。
DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"

PROVIDERS = {
    "volcano": {
        "label": "豆包语音合成（火山引擎）",
        "env_appid": "VOLC_TTS_APPID",
        "env_token": "VOLC_TTS_TOKEN",
    },
    "edge": {
        "label": "edge-tts（免费兜底）",
        "env_appid": "",
        "env_token": "",
    },
}

# 情感可选值，留空就不传，让服务端用音色自带的中性语气
EMOTIONS = ("", "happy", "sad", "angry", "surprised", "fear", "hate", "excited", "coldness", "neutral")

# edge-tts 的兜底音色（免费那档）。语速在渲染层按预设另配，所以这里只管音色名。
# 标签写在页面下拉里：亲切女声（晓晓）/ 轻快女声（晓伊）/ 阳光男声（云希）
# / 播音男声（云扬）/ 磁性男声（云健）。
# 晓夏（卡通风）和两个方言音色没放进来：房源口播要正经，不要喜剧感。
EDGE_VOICES = {
    "female_warm": "zh-CN-XiaoxiaoNeural",
    "female_lively": "zh-CN-XiaoyiNeural",
    "male_warm": "zh-CN-YunxiNeural",
    "male_anchor": "zh-CN-YunyangNeural",
    "male_professional": "zh-CN-YunjianNeural",
}


class TTSError(RuntimeError):
    pass


# ------------------------------------------------------------------ 配置

def _pick(settings: dict, key: str, env: str) -> str:
    value = str(settings.get(key) or "").strip()
    if value:
        return value
    return os.getenv(env, "").strip() if env else ""


def config() -> dict:
    """当前生效的配音配置。ready=False 表示只能走 edge。"""
    settings = get_settings()
    provider = str(settings.get("tts_provider") or "auto").strip().lower()
    if provider not in {"auto", "volcano", "edge"}:
        provider = "auto"
    appid = _pick(settings, "tts_appid", "VOLC_TTS_APPID")
    token = _pick(settings, "tts_token", "VOLC_TTS_TOKEN")
    cluster = str(settings.get("tts_cluster") or "").strip() or "volcano_tts"
    female = str(settings.get("tts_voice_female") or "").strip()
    male = str(settings.get("tts_voice_male") or "").strip()
    return {
        "provider": provider,
        "appid": appid,
        "token": token,
        "cluster": cluster,
        "endpoint": str(settings.get("tts_endpoint") or "").strip() or DEFAULT_ENDPOINT,
        "voice_female": female,
        "voice_male": male,
        "emotion": str(settings.get("tts_emotion") or "").strip(),
        "emotion_scale": _clamp_float(settings.get("tts_emotion_scale"), 1.0, 5.0, 4.0),
        # 只要 appid 和 token 齐了就算可用；音色 id 缺哪个性别就哪个性别退回 edge
        "ready": bool(appid and token),
    }


def voice_for(voice_key: str, conf: dict | None = None) -> str:
    """把 female_warm / male_professional 映射成火山的音色 id，没有就返回空。

    注意别写成 `"male" in key`：female 里也有 male，女声会被判成男声。
    """
    conf = conf or config()
    key = str(voice_key or "").strip().lower()
    is_male = key.startswith("male") or key == "m"
    return conf["voice_male"] if is_male else conf["voice_female"]


def status() -> dict:
    """给首页体检用：现在到底用什么配音、缺什么。"""
    conf = config()
    provider = conf["provider"]
    if provider == "edge":
        return {"provider": "edge", "label": PROVIDERS["edge"]["label"], "ready": True, "detail": "按免费方案配音"}
    if not conf["ready"]:
        detail = "没填火山凭据，先用免费配音；填了就能用豆包音色" if provider == "auto" else "选了豆包语音，但 appid / token 还没填"
        return {"provider": "edge" if provider == "auto" else "volcano", "label": "edge-tts", "ready": provider == "auto", "detail": detail}
    voices = [v for v in (conf["voice_female"], conf["voice_male"]) if v]
    detail = f"音色 {', '.join(voices)}" if voices else "还没填音色 id，会退回免费配音"
    return {"provider": "volcano", "label": PROVIDERS["volcano"]["label"], "ready": bool(voices), "detail": detail}


# ------------------------------------------------------------------ 缓存

def _cache_key(text: str, provider: str, voice: str, rate: str, emotion: str) -> str:
    raw = "|".join((provider, voice, rate, emotion, text))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _cache_paths(key: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"{key}.mp3", CACHE_DIR / f"{key}.json"


def _read_cache(key: str) -> dict | None:
    audio, meta = _cache_paths(key)
    if not (audio.is_file() and audio.stat().st_size > 0 and meta.is_file()):
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {**info, "path": audio}


def _write_cache(key: str, audio: Path, info: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target, meta = _cache_paths(key)
    shutil.copyfile(audio, target)
    meta.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ edge-tts

def _synth_edge(text: str, out_path: Path, voice: str, rate: str) -> None:
    import edge_tts

    async def run() -> None:
        await edge_tts.Communicate(text, voice, rate=rate).save(str(out_path))

    asyncio.run(run())


# ------------------------------------------------------------------ 火山

def _speed_ratio(rate: str) -> float:
    """edge 的 "+10%" 换算成火山的 speed_ratio（1.1），两边共用一套语速设置。"""
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", str(rate or ""))
    if not match:
        return 1.0
    return max(0.5, min(2.0, 1.0 + float(match.group(1)) / 100.0))


def _synth_volcano(text: str, out_path: Path, voice: str, rate: str, conf: dict) -> dict:
    """调一次豆包语音合成，把 base64 音频落到 out_path。返回附加信息（目前是时长）。"""
    payload = {
        "app": {"appid": conf["appid"], "token": conf["token"], "cluster": conf["cluster"]},
        "user": {"uid": "office-video-agent"},
        "audio": {
            "voice_type": voice,
            "encoding": "mp3",
            "speed_ratio": _speed_ratio(rate),
            "volume_ratio": 1.0,
            "pitch_ratio": 1.0,
        },
        "request": {
            "reqid": uuid.uuid4().hex,
            "text": text,
            "text_type": "plain",
            "operation": "query",
        },
    }
    if conf["emotion"] in EMOTIONS and conf["emotion"]:
        payload["audio"]["emotion"] = conf["emotion"]
        payload["audio"]["emotion_scale"] = conf["emotion_scale"]

    try:
        with httpx.Client(timeout=40.0, trust_env=False) as client:
            response = client.post(
                conf["endpoint"],
                json=payload,
                headers={"Authorization": f"Bearer;{conf['token']}"},
            )
    except httpx.HTTPError as error:
        raise TTSError(f"连不上豆包语音：{error}") from error

    if response.status_code != 200:
        raise TTSError(f"豆包语音返回 {response.status_code}：{response.text[:160]}")
    body = response.json()
    code = body.get("code")
    if code not in (3000, 0):
        raise TTSError(f"豆包语音失败 code={code}：{str(body.get('message') or '')[:160]}")
    import base64

    audio = body.get("data")
    if not audio:
        raise TTSError("豆包语音没有返回音频")
    out_path.write_bytes(base64.b64decode(audio))
    return {}


# ------------------------------------------------------------------ 统一入口

def synthesize(
    text: str,
    out_path: str | Path,
    *,
    voice_key: str = "female_warm",
    edge_voice: str = "",
    rate: str = "+0%",
    provider: str = "",
) -> dict:
    """合成一句口播，返回 {path, provider, cached, duration, words}。

    provider 留空时按设置走 auto：能上火山就上，不行悄悄退回 edge。
    显式指定 "volcano" 时不兜底——调试时要知道到底是哪家出的问题。
    """
    text = str(text or "").strip()
    if not text:
        raise TTSError("空文本没有可合成的内容")
    # 房号要逐位念。字幕仍用原文，只有送进引擎的这一份被改写
    spoken = speakable(text)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conf = config()
    chosen = (provider or conf["provider"]).strip().lower()
    voice = voice_for(voice_key, conf)
    if chosen in {"auto", ""}:
        chosen = "volcano" if (conf["ready"] and voice) else "edge"
    if chosen == "volcano" and not (conf["ready"] and voice):
        if (provider or conf["provider"]) == "volcano":
            raise TTSError("选了豆包语音，但 appid / token / 音色 id 没配齐")
        chosen = "edge"
    edge_voice = edge_voice or EDGE_VOICES.get(voice_key, EDGE_VOICES["female_warm"])
    speak_voice = voice if chosen == "volcano" else edge_voice

    key = _cache_key(spoken, chosen, speak_voice, rate, conf["emotion"] if chosen == "volcano" else "")
    cached = _read_cache(key)
    if cached:
        shutil.copyfile(cached["path"], out_path)
        return {"path": out_path, "provider": chosen, "cached": True,
                "duration": float(cached.get("duration") or 0.0), "words": cached.get("words") or []}

    try:
        if chosen == "volcano":
            _synth_volcano(spoken, out_path, speak_voice, rate, conf)
        else:
            _synth_edge(spoken, out_path, speak_voice, rate)
    except Exception as error:  # noqa: BLE001 - 火山挂了要能出片，观感差一点比出不来强
        if (provider or conf["provider"]) != "auto" or chosen == "edge":
            raise
        logger.warning("豆包语音失败，这一句退回 edge-tts：%s", str(error)[:200])
        chosen = "edge"
        speak_voice = edge_voice
        key = _cache_key(spoken, chosen, speak_voice, rate, "")
        cached = _read_cache(key)
        if cached:
            shutil.copyfile(cached["path"], out_path)
            return {"path": out_path, "provider": "edge", "cached": True,
                    "duration": float(cached.get("duration") or 0.0), "words": []}
        _synth_edge(spoken, out_path, speak_voice, rate)

    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise TTSError(f"配音文件是空的：{text[:20]}")
    try:
        duration = probe_duration(out_path)
    except Exception:  # noqa: BLE001 - 量不出时长不影响配音本身，交给调用方按字数估
        duration = 0.0
    info = {"provider": chosen, "voice": speak_voice, "rate": rate, "duration": duration, "words": []}
    try:
        _write_cache(key, out_path, info)
    except OSError as error:
        logger.info("配音缓存写不进去（不影响出片）：%s", error)
    return {"path": out_path, **info, "cached": False}


def clear_cache() -> int:
    """清空配音缓存，返回清掉的条数。"""
    if not CACHE_DIR.is_dir():
        return 0
    count = 0
    for path in CACHE_DIR.glob("*.mp3"):
        try:
            path.unlink()
            path.with_suffix(".json").unlink(missing_ok=True)
            count += 1
        except OSError:
            continue
    return count


def _clamp_float(value, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
