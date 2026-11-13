"""风格成片渲染：把「干净素材 + 角标 + 字幕 + 口播」按模板合成一条片子。

输入是一个 storyboard：每句文案、显示时间、可选的功能区角标，以及从素材里
选哪一段画面。渲染分四层：

1. **画面** —— 智能选镜（shot_select）挑干净段，Ken Burns 缓推/缓拉
   交替让空镜动起来，轻微提亮提饱和；
2. **底部字幕** —— 模板里学来的样式：白色特粗体、黑描边、居底居中，
   折行优先在标点处断，不会把「一套」孤零零甩到第二行；
3. **顶部角标** —— 功能区名 + 一句话卖点，左上两行；
4. **声音** —— audio_mode 自己选：
   mix     原声压低当 BGM + 逐句口播（默认）
   original 只保留素材原声（不合成口播，字幕照上）
   voice    只口播，干净人声
   mute     无声，适合发平台前再自己配

字体直接用剪映工程里提取的那两个（Noto Sans SC Black / 锐字真言体），
跟他们手工剪辑的片子保持同款观感。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import bgm, flower_text, tts
from .asr import _windows_path
from .db import get_settings, utc_now
from .shot_select import pick_clips
from .shots import ShotError, _find_binary

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_RENDER_DIR = BASE_DIR / "data" / "render-env"

# 剪映工程里提取的字体（跟他们手剪的片子同款）
_JIANYING_FONTS = Path(r"D:\剪映成品视频\wonderfulVideos14770298223804407620\edit")
# 剪映工程里的字体是 CFF 轮廓的 OTF，gyan 版 ffmpeg 的 freetype 读到会直接段错误，
# 所以预先转成 TTF（tools/fetch_fonts.py 生成）放在 data/render-env 下。
FONT_BLACK = BASE_DIR / "data" / "render-env" / "NotoSansSCBlack.ttf"
FONT_BODY = BASE_DIR / "data" / "render-env" / "NotoSansSCBold.ttf"
FONT_FALLBACK = Path(r"C:\Windows\Fonts\msyhbd.ttc")

TTS_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_RATE = "+10%"          # 中介口播普遍偏快，慢了反而假
# 1080 宽实测：字号 × 0.69 ≈ 单字宽度。120 号字一行 10 个汉字占画面约 78%，
# 字高 79px（1920 高的 4%），手机上抬眼就能看清。
CAPTION_SIZE = 120
# 设置页给的是「小 / 中 / 大」，这里落成 1080 画布上的字号。
# 单行字数由字号反算（见 build_ass），字号越大一行越少，永远不会出血。
CAPTION_SIZE_CHOICES = {"small": 100, "medium": 120, "large": 140}
# 字幕距画面底边：靠上 / 居中 / 靠下
CAPTION_BOTTOM_CHOICES = {"high": 380, "middle": 260, "low": 170}
# 花字频率：整条片子最多放几处
FLOWER_DENSITY_CHOICES = {"low": 1, "normal": 2, "high": 3}
CAPTION_BORDER = 6
CAPTION_MARGIN_V = 260      # 字幕距画面底边的距离（1080x1920 画布）
LABEL_SIZE = 78
LABEL_X = 90
LABEL_Y = 340

AUDIO_MODES = {"mix", "original", "voice", "mute"}

# 成片最后定格多少秒。定格帧是干净的（字幕已结束），正好给人截图、或者
# 让画面停在最有说服力的那一格上；不留反而显得"话没说完就切走了"。
END_HOLD = 1.5

# Ken Burns：缓推到 1.12 倍再缓拉回来，交替使用，空镜就有呼吸感
ZOOM_IN = 0.12             # 最大推近幅度
CANVAS_W, CANVAS_H = 1080, 1920
CANVAS_WIDE_W, CANVAS_WIDE_H = 1920, 1080


class RenderError(RuntimeError):
    pass


def _ffmpeg() -> str:
    try:
        return _find_binary("ffmpeg")
    except ShotError as error:
        raise RenderError(str(error)) from error


def _ffprobe() -> str:
    try:
        return _find_binary("ffprobe")
    except ShotError as error:
        raise RenderError(str(error)) from error


def media_duration(path: str | Path) -> float:
    """容器时长（音轨常常比视频流短，做节奏分配不能用这个）。"""
    completed = subprocess.run(
        [_ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", _windows_path(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(completed.stdout.strip())
    except ValueError as error:
        raise RenderError(f"读不到时长：{path}") from error


def video_duration(path: str | Path) -> float:
    """视频流时长：节奏分配、收口都以画面为准。"""
    completed = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=duration", "-of", "csv=p=0", _windows_path(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        value = float(completed.stdout.strip())
    except ValueError as error:
        raise RenderError(f"读不到视频流时长：{path}") from error
    return value


def _has_audio(path: Path) -> bool:
    completed = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", _windows_path(path)],
        capture_output=True, text=True, check=False,
    )
    return bool(completed.stdout.strip())


# ------------------------------------------------------------------ 文案转语音

# 价格数字前垫的静音长度：0.25 秒——一拍不长不短，念完引导词正好换气
PRICE_PAUSE = 0.25


def _trim_silence(src: Path, dst: Path, lead: float = 0.05, tail: float = 0.08) -> bool:
    """掐头去尾。失败/掐过头时返回 False，调用方用原文件兜底。

    edge-tts 每段自带 ~0.17 秒头部空白和 0.5~1.2 秒尾部空白
    （实测「一个月」三个字给了 1.87 秒）。不掐的话：
    - 价格前垫 0.25 秒，垫出来是 1.5 秒的洞；
    - 句间停顿全被这些空白吃掉，「停顿按语义给」成了一句空话。
    start_silence 各留一点点头尾，让声音不贴着上一拍出来。
    """
    chain = (
        f"silenceremove=start_periods=1:start_threshold=-40dB:start_silence={lead},"
        "areverse,"
        f"silenceremove=start_periods=1:start_threshold=-40dB:start_silence={tail},"
        "areverse"
    )
    completed = subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-af", chain, "-c:a", "libmp3lame", "-q:a", "4", str(dst)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        return False
    try:
        return media_duration(dst) >= 0.15
    except RenderError:
        return False


def _concat_speech(pieces: list[Path], out_path: Path, pause: float = PRICE_PAUSE) -> Path:
    """把分段口播中间垫静音接成一句。

    静音用 anullsrc 现造（atrim 截到 pause 秒），不用预生成静音文件——
    垫的时长以后要调，改一个常量就行。
    """
    if len(pieces) < 2:
        raise RenderError("分段拼接至少要两段音频")
    inputs: list[str] = []
    for piece in pieces:
        inputs += ["-i", str(piece)]
    chains: list[str] = []
    labels: list[str] = []
    for index in range(len(pieces)):
        chains.append(f"[{index}:a]aresample=48000,aformat=channel_layouts=stereo[a{index}]")
        labels.append(f"[a{index}]")
        if index < len(pieces) - 1:
            chains.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{pause:.2f}[p{index}]")
            labels.append(f"[p{index}]")
    chains.append("".join(labels) + "concat=n=%d:v=0:a=1[out]" % len(labels))
    completed = subprocess.run(
        [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", ";".join(chains), "-map", "[out]",
         "-c:a", "libmp3lame", "-q:a", "4", str(out_path)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0 or not out_path.is_file():
        raise RenderError(f"分段口播拼接失败：{completed.stderr[-200:]}")


# 一句最多比自然节奏多喘这么多秒。目标时长比内容长时靠它兜住节奏
MAX_BREATH = 1.0


def _sentence_windows(spoken: list[float], pauses: list[float], goal: float | None) -> list[float]:
    """每句的时间窗。恒 ≥ 语音+停顿；多出来的呼吸均摊，但有上限。

    没有上限时，17 秒的内容硬拉到 30 秒目标，每个句间都是 2 秒多的空洞——
    观众在第一个空洞就划走了，比成片短一点伤得多。内容不够就短一点。
    """
    count = len(spoken)
    if not count:
        return []
    natural = sum(s + p for s, p in zip(spoken, pauses))
    share = 0.0
    if goal and goal > natural:
        share = min((goal - natural) / count, MAX_BREATH)
    return [s + p + share for s, p in zip(spoken, pauses)]


def _pause_after(index: int, count: int, text: str) -> float:
    """说完这句，下一句开口前停多久。

    以前固定 0.6 秒一刀切：钩子刚说完下一句就抢着开口，收尾那句前
    也没留拍。按语义给——钩子留一拍让人决定不划走，收尾前留一拍
    让人把前面听进去，问句/叹句比陈述句停得长，从句只是喘口气。
    """
    if index + 1 >= count:      # 最后一句：尾留一点呼吸给定格帧
        return 0.4
    if index == 0:              # 钩子说完留一拍
        return 0.75
    if index + 2 == count:      # 收尾前留一拍
        return 0.7
    tail = (text or "").rstrip()[-1:]
    if tail in "！？":
        return 0.65
    if tail in "，、；":
        return 0.35
    return 0.5


def synth_speech(
    text: str,
    out_path: Path,
    voice: str = TTS_VOICE,
    rate: str = TTS_RATE,
    voice_key: str = "",
) -> Path:
    """合成一句口播，返回掐头去尾后的音频。

    edge-tts 每段自带 0.17s 头 / 0.5~1.2s 尾的空白，先掐掉再进时间轴，
    句间停顿和价格前的垫才作数。句子里出现价格时按金额切点分段合成，
    中间垫 PRICE_PAUSE 秒静音——「一个月2600」连着念会糊成一串。
    真正的合成在 app/tts.py：配了火山凭据就用豆包音色，没配或调用失败
    自动退回 edge-tts。voice 是 edge 的音色名，voice_key 是女/男预设，
    用来映射豆包那边的 speaker id。
    """
    segments: list[Path] = []
    for number, piece in enumerate(tts.price_splits(text) or [text]):
        raw = out_path.parent / f"{out_path.stem}_raw{number}.mp3"
        tts.synthesize(
            piece, raw,
            voice_key=voice_key or "female_warm",
            edge_voice=voice,
            rate=rate,
        )
        tight = out_path.parent / f"{out_path.stem}_t{number}.mp3"
        segments.append(tight if _trim_silence(raw, tight) else raw)
    if len(segments) == 1:
        shutil.copyfile(segments[0], out_path)
    else:
        _concat_speech(segments, out_path)
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RenderError(f"语音合成失败：{text[:20]}")
    return out_path


# ------------------------------------------------------------------ drawtext

def _escape(text: str) -> str:
    """drawtext 的文本要转义 : ' \\ % , 这几个字符。"""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


_PUNCT = "，、。！？；：,.!?;: "


def _wrap(text: str, per_line: int = 12) -> str:
    """老的两行折行器：只留给 drawtext 时代的历史调用，字幕已改单行分段。

    现在的字幕走 caption_segments()——长句切成短段按时间轴依次出，
    拼成两行反而更难读（字号得缩、还得上下跳）。
    """
    if len(text) <= per_line:
        return text
    lines: list[str] = []
    remaining = text
    while len(remaining) > per_line:
        window = remaining[: per_line + 1]
        cut = -1
        for i in range(len(window) - 1, 3, -1):   # 至少留 4 个字在下一行
            if window[i - 1] in _PUNCT:
                cut = i
                break
        if cut <= 0:
            cut = per_line
        lines.append(remaining[:cut].rstrip(_PUNCT))
        remaining = remaining[cut:].lstrip(_PUNCT)
    if remaining:
        lines.append(remaining)
    return "\n".join(lines)


def _font_param(font: str) -> str:
    """fontfile 只需要转义盘符冒号，路径统一用正斜杠。"""
    return font.replace("\\", "/").replace(":", "\\:")


# ------------------------------------------------------------------ 字幕（ASS）

# 为什么不用 drawtext：它只能整句一个颜色。中介的片子里「120 平」「3 隔」
# 这类数字是卖点，得挑出来变色，drawtext 做不到；而 libass 一行标签就解决了，
# 顺带把描边、行距、位置都交给样式表管，不用在代码里拼字符串。
_HIGHLIGHT_OPEN = r"{\c&H00D7FF&}"    # 金色（ASS 是 BGR：00D7FF = #FFD700）
_HIGHLIGHT_CLOSE = r"{\c&HFFFFFF&}"

# 单价、面积、格局、楼层这些才是看房的人会抓的词，其余一律白字
_KEYWORD = re.compile(
    r"\d+(?:\.\d+)?\s*(?:平方米|平方|平米|平|㎡|元/平方米|元/平|元|万|层|个工位|工位|隔|年|号线|号楼)"
)

# 手动标记：【拎包入驻】写在文案里 → 字幕里高亮、并当作花字候选
_MARK = re.compile(r"【([^】\n]{1,12})】")


def parse_marks(text: str) -> tuple[str, list[str]]:
    """把【词】标记从文案里摘出来。返回 (干净文案, 标记词列表)。"""
    marks: list[str] = []

    def _keep(match: re.Match) -> str:
        marks.append(match.group(1))
        return match.group(1)

    clean = _MARK.sub(_keep, str(text or ""))
    return clean, marks


def _ass_text(text: str) -> str:
    """ASS 的正文要转义，换行用 \\N，大括号是标签起始符。"""
    return (
        text.replace("\\", "＼")
        .replace("{", "（")
        .replace("}", "）")
    )


def _highlight(text: str, limit: int = 2, marks: tuple[str, ...] = (), regex: bool = True) -> str:
    """给一句话里最值钱的词上色，一句最多两处——多了就成花瓜了。

    优先级：手动/AI 给的词 > 正则挑数字卖点。regex=False 时只认给的词，
    对应设置里的「只用【】标记」。
    """
    escaped = _ass_text(text)
    if marks:
        count = 0
        for word in marks:
            piece = _ass_text(word)
            if piece and piece in escaped and count < limit:
                escaped = escaped.replace(piece, f"{_HIGHLIGHT_OPEN}{piece}{_HIGHLIGHT_CLOSE}", 1)
                count += 1
        if count:
            return escaped
    if not regex:
        return escaped

    used = 0

    def wrap(match: re.Match) -> str:
        nonlocal used
        if used >= limit:
            return match.group(0)
        used += 1
        return f"{_HIGHLIGHT_OPEN}{match.group(0)}{_HIGHLIGHT_CLOSE}"

    return _KEYWORD.sub(wrap, escaped)


def _ass_time(seconds: float) -> str:
    seconds = max(seconds, 0.0)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


# ------------------------------------------------------------------ 字幕分段
#
# 一条字幕铺满屏幕两行是最难看的：字号得缩、还得上下跳。中介刷到的爆款
# 全是「一行、按语速一句一句换」。所以一句话不再折行，而是切成若干短段，
# 每段单行、在自己那一小段时间里出现，念到哪显示到哪。

CAPTION_MAX_CHARS = 10      # 120 号字一行 10 个汉字，左右各留一百多像素，不出血

# 段尾停顿标点：切在这里最自然；字幕里不显示，读起来更干净
_CAPTION_BREAK = "，。！？；：、,.!?;:"
_CAPTION_TRIM = "，。、；：,.、;:"
# 量词结尾处断开，别把「230平」拆成「230 / 平」
_MEASURE_TAIL = "平㎡元万层隔年号个位套间月天"


def _soft_cut(piece: str, limit: int) -> int:
    """超长段硬切的落点：优先切在量词后面，切不到才按字数切。"""
    lowest = max(4, limit - 4)
    for cut in range(limit, lowest, -1):
        if cut < len(piece) and piece[cut - 1] in _MEASURE_TAIL:
            return cut
    return limit


def caption_segments(text: str, max_chars: int = CAPTION_MAX_CHARS) -> list[tuple[str, float]]:
    """把一句话切成单行字幕段，返回 [(文字, 时长权重)]。

    权重 ≈ 字数，段尾有停顿标点的多算一点，因为 TTS 会在那里顿一下。
    """
    raw: list[tuple[str, bool]] = []
    buffer = ""
    content = str(text or "").strip()
    for index, char in enumerate(content):
        buffer += char
        if char not in _CAPTION_BREAK:
            continue
        # 「3.5 元」「1,200」里的小数点和千分位不是句子结束，别切在这里
        if char in ".," and (
            (index + 1 < len(content) and content[index + 1].isdigit())
            or (index > 0 and content[index - 1].isdigit())
        ):
            continue
        if len(buffer.strip(_CAPTION_TRIM)) >= 2:
            raw.append((buffer, True))
            buffer = ""
    if buffer.strip():
        raw.append((buffer, False))

    segments: list[tuple[str, bool]] = []
    for piece, pause in raw:
        piece = piece.strip().rstrip(_CAPTION_TRIM)
        if not piece:
            continue
        while len(piece) > max_chars:
            # 只超一点就对半分，别切出「12 字 + 2 字」的小尾巴
            cut = (len(piece) + 1) // 2 if len(piece) <= max_chars + 4 else _soft_cut(piece, max_chars)
            segments.append((piece[:cut].rstrip(_CAPTION_TRIM), False))
            piece = piece[cut:].lstrip(_CAPTION_TRIM)
        if piece:
            segments.append((piece, pause))

    # 太短的尾巴（「平」「另外」）并到前面，别让字幕一闪就换
    merged: list[tuple[str, bool]] = []
    for piece, pause in segments:
        if merged and len(piece) <= 2 and len(merged[-1][0]) + len(piece) <= max_chars:
            merged[-1] = (merged[-1][0] + piece, pause)
        else:
            merged.append((piece, pause))
    return [(piece, len(piece) + (1.2 if pause else 0.0)) for piece, pause in merged]


def segment_times(start: float, end: float, weights: list[float]) -> list[tuple[float, float]]:
    """把一句话的时间窗按权重切开，段与段首尾相接、不留缝。"""
    total = sum(weights) or float(len(weights) or 1)
    span = max(float(end) - float(start), 0.0)
    times: list[tuple[float, float]] = []
    cursor = float(start)
    for weight in weights:
        step = span * (weight / total)
        times.append((round(cursor, 2), round(cursor + step, 2)))
        cursor += step
    if times:
        times[-1] = (times[-1][0], round(float(end), 2))
    return times


def build_ass(
    sentences: list[dict],
    canvas: tuple[int, int],
    font_name: str,
    size: int = CAPTION_SIZE,
    margin_v: int = CAPTION_MARGIN_V,
    highlight: bool = True,
    highlight_regex: bool = True,
) -> str:
    """把分镜写成一份 ASS 字幕文件。

    一句话切成若干单行字幕段，一段一条 Dialogue，按语音时间轴依次出现：
    念到哪显示到哪，屏幕上始终只有一行字。

    size 以 1080 宽为基准，16:9 会按画布宽度等比放大；单行字数由字号反算，
    字号调大就自动少放几个字，不会出血。
    highlight=False 时全部白字（有人不喜欢变色）。
    """
    width, height = canvas
    scale = width / 1080
    size = max(40, int(round(size * scale)))
    margin_v = int(round(margin_v * scale))
    side = int(round(70 * scale))
    per_char = size * 0.694          # 实测：单字宽 ≈ 字号 × 0.694
    max_chars = max(5, int((width - 2 * side) / per_char))
    border = max(3, int(round(CAPTION_BORDER * scale)))
    label_size = int(round(LABEL_SIZE * scale))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font_name},{size},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,{border},3,2,{side},{side},{margin_v},1
Style: Label,{font_name},{label_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,5,4,7,{LABEL_X},{LABEL_X},{LABEL_Y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for sentence in sentences:
        start = float(sentence["start"])
        end = float(sentence["end"])
        # 【词】手动标记优先，其次 AI 挑的词，都没有才走正则
        clean, inline_marks = parse_marks(sentence["text"])
        words = tuple(sentence.get("marks") or inline_marks or ())
        if not words:
            words = tuple(w for w in (sentence.get("highlights") or []) if w in clean)
        segments = caption_segments(clean, max_chars=max_chars)
        if segments:
            # 有真实语音时长就贴着语音走：目标时长会把画面拉长，字幕不能跟着拖，
            # 否则念完半分钟了字还在屏幕上慢慢挪。
            spoken = float(sentence.get("spoken") or 0.0)
            # +0.45 是为了接住句间那点空隙：字幕刚消失下一句才起，会闪一下空白
            speech_end = min(end, start + spoken + 0.45) if spoken > 0 else end
            for (piece, _weight), (seg_start, seg_end) in zip(
                segments, segment_times(start, speech_end, [w for _, w in segments])
            ):
                body = (
                    _highlight(
                        piece,
                        marks=tuple(w for w in words if w in piece),
                        regex=highlight_regex,
                    )
                    if highlight else _ass_text(piece)
                )
                lines.append(
                    f"Dialogue: 0,{_ass_time(seg_start)},{_ass_time(seg_end)},Cap,,0,0,0,,{body}"
                )
        label = sentence.get("label") or {}
        if label.get("title"):
            title = _ass_text(label["title"])
            sub = _ass_text(label.get("sub") or "")
            body = f"{title}\\N{sub}" if sub else title
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Label,,0,0,0,,{body}")
    return header + "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 画面滤镜

def _zoom_filter(item: dict, index: int, fps: int, canvas: tuple[int, int] = (CANVAS_W, CANVAS_H)) -> str:
    """一段画面的滤镜链：截段 → 放慢 → Ken Burns → 调色 → 规格。

    zoompan 交替缓推/缓拉：偶数句推近（1.00→1.12），奇数句拉远
    （1.12→1.00）。先放大 1.25 倍做工作分辨率，推拉时不会露边。
    """
    width, height = canvas
    clip_start, clip_end = item["clip"]
    duration = max(item["end"] - item["start"], 0.5)
    frames = max(int(duration * fps), 8)
    step = ZOOM_IN / frames
    if index % 2 == 0:
        zoom_expr = f"min(1+{step:.5f}*on,1.12)"
    else:
        zoom_expr = f"max(1.12-{step:.5f}*on,1.0)"
    # 推拉围绕画面重心
    x_expr = "iw/2-(iw/zoom/2)"
    y_expr = "ih/2-(ih/zoom/2)"
    return (
        f"trim=start={clip_start:.2f}:end={clip_end:.2f},setpts=(PTS-STARTPTS)/{item['slow']:.4f},"
        f"fps={fps},"
        f"scale={int(width * 1.25)}:{int(height * 1.25)},"
        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d=1:s={width}x{height}:fps={fps},"
        f"eq=brightness=0.02:saturation=1.10:contrast=1.04,"
        f"setsar=1"
    )


# ------------------------------------------------------------------ 花字层


# 中介文案里三不五时出现的卖点词：没有数字可抓时，花字就从这里挑。
# 顺序即优先级——「拎包入驻」比「朝南」更值得做成大字。
_SELLING_TERMS = (
    "拎包入驻", "精装修", "新装修", "整层", "独用", "可分割", "可注册",
    "随时看房", "免佣", "近地铁", "朝南", "采光通透", "带家具",
    "独立空调", "24小时", "带车位", "大开间", "层高高", "配套齐全",
)


def _flower_phrase(sentence: dict) -> str:
    """从一句文案里挑花字短语：手动【标记】> AI 挑的词 > 卖点词 > 数字。

    花字必须是 2~6 个字、不带标点——这是 flower_text 的硬约束，
    「230 平」「拎包入驻」合格，「紧邻 3 号线地铁口」不合格。
    """
    def _ok(word: str) -> bool:
        word = (word or "").strip()
        return 2 <= len(word) <= 6 and not flower_text.has_punct(word)

    for word in list(sentence.get("marks") or ()) + list(sentence.get("highlights") or ()):
        if _ok(word):
            return word.strip()
    text = sentence["text"] or ""
    for term in _SELLING_TERMS:
        if term in text:
            return term
    match = _KEYWORD.search(text)
    if match:
        phrase = re.sub(r"\s+", "", match.group(0))
        if _ok(phrase):
            return phrase
    return ""


def _flower_windows(sentences: list[dict], density: int = 2, style: str = "auto") -> list[dict]:
    """挑花字，给出显示窗口。

    花字不是字幕的复读机：候选词从整条文案里收集，落点放在开场和
    中段——观众先进眼「230平」，几秒后才听到念这句，比同屏重复更有用。
    density 控制整条最多放几处；style 选 auto 就按短语哈希在常用样式里轮换
    （重渲结果一致），选了具体样式就全套统一。
    """
    if not flower_text.fonts_ready() or not sentences:
        return []
    phrases: list[str] = []
    for sentence in sentences:
        phrase = _flower_phrase(sentence)
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    if not phrases:
        return []

    slots = [i for i, s in enumerate(sentences) if s["end"] - s["start"] >= 1.2]
    if not slots:
        return []

    windows: list[dict] = []
    taken: set[int] = set()
    pool = flower_text.HOUSE_STYLES
    if style and style != "auto" and style in flower_text.STYLES:
        pool = (style,)
    for phrase in phrases[: max(1, int(density))]:
        # 落点优先级：说出这句的前一句（预告）→ 后一句（回味）→ 本句 → 任意空位。
        # 同屏出现「字幕正在念 + 花字写着同样的词」属于浪费画面。
        mention = next((i for i, s in enumerate(sentences) if phrase in s["text"]), None)
        order: list[int] = []
        if mention is not None:
            if mention - 1 >= 0:
                order.append(mention - 1)
            if mention + 1 < len(sentences):
                order.append(mention + 1)
            order.append(mention)
        order += slots
        index = next((i for i in order if i in slots and i not in taken), None)
        if index is None:
            continue
        taken.add(index)
        slot = sentences[index]
        start = slot["start"] + 0.15
        duration = min(1.8, max(1.0, (slot["end"] - slot["start"]) * 0.5))
        end = min(slot["end"], start + duration)
        if end - start < 0.3:
            continue
        pool_style = pool
        digest = hashlib.sha256(phrase.encode("utf-8")).hexdigest()
        windows.append({
            "text": phrase,
            "style": pool_style[int(digest[:8], 16) % len(pool_style)],
            "start": start,
            "end": end,
        })
    return windows


def _flower_chains(
    windows: list[dict], work: Path, canvas: tuple[int, int], first_input: int, prev_label: str,
) -> tuple[list[str], list[str], str]:
    """生成花字的输入与滤镜链。返回 (追加输入, 滤镜链, 结束标签)。

    画不出来的（字体缺失、短语不合格）直接跳过，绝不拖垮整条出片。
    """
    width, height = canvas
    inputs: list[str] = []
    chains: list[str] = []
    prev = prev_label
    rendered: list[dict] = []
    for item in windows:
        png = work / f"flower-{len(rendered):02d}.png"
        try:
            image = flower_text.render(item["text"], item["style"])
            image.save(png, "PNG")
        except Exception as error:  # noqa: BLE001 - 花字失败只损失花字
            logger.info("花字跳过 %r：%s", item["text"], error)
            continue
        rendered.append({**item, "png": png, "size": image.size})
        # 花字没出现时，先看这行时间对不对：多半是落在没人看的那几帧里
        logger.info(
            "花字 %s（%s）%.2f–%.2fs", item["text"], item["style"], item["start"], item["end"],
        )
    for n, item in enumerate(rendered, start=1):
        index = first_input + n - 1
        w, h = item["size"]
        limit = int(width * 0.82) if item["style"] in ("glow_title", "bottom_glow") else int(width * 0.44)
        if w > limit:
            ratio = limit / w
            w, h = int(w * ratio), int(h * ratio)
        a, b = item["start"], item["end"]
        inputs += ["-loop", "1", "-i", str(item["png"])]
        chains.append(
            f"[{index}:v]format=rgba,"
            f"fade=t=in:st={a:.3f}:d=0.20:alpha=1,"
            f"fade=t=out:st={max(a, b - 0.18):.3f}:d=0.15:alpha=1,"
            f"scale={w}:{h}[fl{n}]"
        )
        if item["style"] == "glow_title":
            x = (width - w) // 2
            y = int(height * 0.08)
        else:
            x = width - w - 70
            y = int(height * 0.40)
        # 别压到底部字幕区
        floor = height - 420 - h
        y = max(40, min(y, floor if floor >= 40 else height - h - 40))
        label = f"[fv{n}]"
        chains.append(
            f"{prev}[fl{n}]overlay=x={x}:y={y}:enable='gte(t,{a:.3f})*lt(t,{b:.3f})'{label}"
        )
        prev = label
    return inputs, chains, prev


# ------------------------------------------------------------------ 渲染

def _fontconfig_env() -> dict:
    """gyan 版 ffmpeg 自带 fontconfig 但没有配置文件，不补会直接报错。"""
    import os

    conf = DATA_RENDER_DIR / "fonts.conf"
    if not conf.is_file():
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        cache = DATA_RENDER_DIR / "fc-cache"
        cache.mkdir(parents=True, exist_ok=True)
        conf.write_text(
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
            "<fontconfig>\n"
            f"  <dir>{fonts_dir}</dir>\n"
            f"  <cachedir>{cache}</cachedir>\n"
            "</fontconfig>\n",
            encoding="utf-8",
        )
    return {"FONTCONFIG_FILE": str(conf)}


def render_storyboard(
    storyboard: list[dict],
    source: str | Path,
    out_path: str | Path,
    bgm_volume: float = 0.30,
    fps: int = 30,
    audio_mode: str = "mix",
    tts_voice: str = TTS_VOICE,
    tts_voice_key: str = "female_warm",
    tts_rate: str = TTS_RATE,
    kenburns: bool = True,
    canvas: tuple[int, int] = (CANVAS_W, CANVAS_H),
    goal_seconds: float | None = None,
    flower: bool = True,
    flower_density: int = 2,
    flower_style: str = "auto",
    music_path: str | Path | None = None,
    music_volume: float = 0.14,
    caption_size: int = CAPTION_SIZE,
    caption_bottom: int = CAPTION_MARGIN_V,
    caption_highlight: bool = True,
    caption_highlight_regex: bool = True,
    end_hold: float = END_HOLD,
) -> Path:
    """把 storyboard 渲染成片。storyboard 每项：

    {"text": "口播", "label": {"title": "工区", "sub": "可容纳20人办公"} 或省略,
     "marks": ["手动高亮词"] 或省略, "highlights": ["AI 挑的词"] 或省略}

    goal_seconds：目标成片时长。给了就按它铺镜头（短文案多留空镜），
    不给就按口播的自然长度走。

    audio_mode：
    - mix      原声压低铺底 + 口播 + BGM（默认）
    - original 只保留素材原声（不合成口播，渲染更快）
    - voice    只口播（给了 BGM 就带 BGM）
    - mute     无声

    flower：叠花字层（卖点短语画成贴片，Pillow 本地画，不依赖模型）。
    music_path：BGM 音轨；与素材原声是两路，互不影响，None 不加。
    """
    if audio_mode not in AUDIO_MODES:
        raise RenderError(f"audio_mode 只能是 {'/'.join(sorted(AUDIO_MODES))}，收到 {audio_mode!r}")
    want_voice = audio_mode in {"mix", "voice"}

    source = Path(_windows_path(source))
    if not source.is_file():
        raise RenderError(f"素材不存在：{source}")
    if not _has_audio(source) and audio_mode in {"mix", "original"}:
        audio_mode = "voice" if len(storyboard) else "mute"
        want_voice = audio_mode == "voice"
    ffmpeg = _ffmpeg()
    # 字体：给 libass 的是「字体家族名」，它自己去 fontsdir 里找匹配的
    if FONT_BLACK.is_file():
        font_family = "Noto Sans SC Black"
    elif FONT_BODY.is_file():
        font_family = "Noto Sans SC"
    else:
        font_family = "Microsoft YaHei"

    work = Path(tempfile.mkdtemp(prefix="ova-render-"))

    # 1) 逐句合成口播，定每句的时间窗（时长向目标时长靠，但不超过 10 秒/句）
    sentences: list[dict] = []
    for index, item in enumerate(storyboard):
        # 【词】手动标记：字幕高亮 + 花字候选都用它；TTS 念干净文案
        clean, inline_marks = parse_marks(str(item.get("text") or ""))
        speech = None
        spoken = 0.0
        if want_voice:
            speech = synth_speech(
                clean, work / f"tts{index:02d}.mp3",
                voice=tts_voice, rate=tts_rate, voice_key=tts_voice_key,
            )
            spoken = media_duration(speech)
        sentences.append({
            "text": clean,
            "label": item.get("label") or {},
            "marks": [m for m in (item.get("marks") or inline_marks or []) if m in clean],
            "highlights": [w for w in (item.get("highlights") or []) if w in clean],
            "speech": speech,
            "spoken": spoken,
        })

    if want_voice:
        count = len(sentences)
        # 句间停顿按语义给，替换掉以前固定 0.6 一刀切：
        # 钩子说完要留拍、收尾前要留拍、从句只喘口气。
        for index, sentence in enumerate(sentences):
            sentence["pause_after"] = _pause_after(index, count, sentence["text"])
        # 窗口恒 ≥ 语音+停顿（下一句不会跟这句叠上）；目标时长的呼吸有上限，
        # 内容不够就短一点，不硬拉出满片空洞
        windows = _sentence_windows(
            [s["spoken"] for s in sentences],
            [s["pause_after"] for s in sentences],
            goal_seconds,
        )
        for sentence, window in zip(sentences, windows):
            sentence["duration"] = window
    else:
        duration_total = video_duration(source)
        even = min(max((goal_seconds or duration_total / max(len(storyboard), 1)), 1.6), 10.0)
        for sentence in sentences:
            sentence["duration"] = even

    cursor = 0.0
    for sentence in sentences:
        sentence["start"] = round(cursor, 2)
        sentence["end"] = round(cursor + sentence["duration"], 2)
        cursor += sentence["duration"]
    total = round(cursor, 2)

    # 1.4) 尾帧定格：最后一帧克隆出来铺满 hold 秒。时长要算进 total，
    # 否则音轨比画面短一截，-shortest 会把定格帧砍掉。
    hold = max(0.0, float(end_hold or 0.0))
    total = round(total + hold, 2)

    # 1.5) 镜头分配：每 3 秒左右一个镜头，一句长文案拆几个空镜，画面不干等
    shots_needed = sum(max(1, round(sentence["duration"] / 3.0)) for sentence in sentences)
    clips = pick_clips(source, shots_needed)
    plan: list[dict] = []
    clip_index = 0
    for sentence in sentences:
        count = max(1, round(sentence["duration"] / 3.0))
        span = sentence["duration"] / count
        for piece in range(count):
            clip_start, clip_end = clips[clip_index % len(clips)]
            clip_index += 1
            clip_len = clip_end - clip_start
            plan.append({
                "start": round(sentence["start"] + piece * span, 2),
                "end": round(sentence["start"] + (piece + 1) * span, 2),
                "clip": (clip_start, clip_end),
                "slow": clip_len / span if span else 1.0,
                # 字幕/角标只在句首那个镜头开始前挂，避免每段重复写 drawtext
                "sentence": sentence if piece == 0 else None,
            })

    # 2) 拼滤镜：截段 → 变速 → Ken Burns → 接力，再叠花字，最后烧字幕角标
    flower_windows = (
        _flower_windows(sentences, density=flower_density, style=flower_style) if flower else []
    )
    # 输入顺序：[0] 素材，[1..N] 口播，接着花字 PNG，最后 BGM
    speech_count = len(sentences) if want_voice else 0
    flower_inputs, flower_chains, flower_last = _flower_chains(
        flower_windows, work, canvas, 1 + speech_count, "[base]",
    )
    music_input_index = 1 + speech_count + (len(flower_inputs) // 4)

    inputs = ["-i", str(source)]
    if want_voice:
        for sentence in sentences:
            inputs += ["-i", str(sentence["speech"])]
    inputs += flower_inputs
    use_music = (
        music_path is not None and Path(music_path).is_file()
        and audio_mode in {"mix", "voice"}
    )
    if use_music:
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]

    video_parts = []
    for index, item in enumerate(plan):
        chain = _zoom_filter(item, index, fps, canvas) if kenburns else (
            f"trim=start={item['clip'][0]:.2f}:end={item['clip'][1]:.2f},"
            f"setpts=(PTS-STARTPTS)/{item['slow']:.4f},fps={fps},"
            f"scale={canvas[0]}:{canvas[1]},eq=brightness=0.02:saturation=1.10:contrast=1.04,setsar=1"
        )
        # 只定格最后一帧：接在最后一段后面，clone 出来的还是它自己的末帧
        if hold > 0 and index == len(plan) - 1:
            chain += f",tpad=stop_mode=clone:stop_duration={hold:.2f}"
        video_parts.append(f"[0:v]{chain}[v{index}]")
    concat_in = "".join(f"[v{i}]" for i in range(len(plan)))
    video_parts.append(f"{concat_in}concat=n={len(plan)}:v=1:a=0[base]")

    # 2.5) 花字层叠在画面上（不进字幕轨：它带形状和渐变，ASS 画不出来）
    if flower_chains:
        video_parts += flower_chains
    base_label = flower_last

    # 2.6) 字幕：写一份 ASS 再一次性烧上去。
    # 文件名和 fontsdir 都用相对路径、并把 cwd 指到 data/render-env：
    # ffmpeg 的滤镜参数里冒号是分隔符，Windows 盘符会把参数切碎。
    ass_name = f"sub-{uuid.uuid4().hex[:8]}.ass"
    ass_path = DATA_RENDER_DIR / ass_name
    DATA_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(
        build_ass(
            sentences, canvas, font_family,
            size=caption_size, margin_v=caption_bottom,
            highlight=caption_highlight, highlight_regex=caption_highlight_regex,
        ),
        encoding="utf-8",
    )
    video_parts.append(f"{base_label}ass={ass_name}:fontsdir=.[vout]")

    # 3) 声音：按 audio_mode 决定原声、口播、BGM 的去留
    def _music_label() -> tuple[str, str]:
        """BGM 一路：截到片长、限音量、结尾淡出。返回 (标签, 滤镜行)。"""
        label = f"[{music_input_index}:a]"
        chain = (
            f"{label}atrim=0:{total},volume={music_volume:.2f},"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"apad=whole_dur={total},afade=t=out:st={max(0.0, total - 0.8):.2f}:d=0.8[mus]"
        )
        return "[mus]", chain

    audio_parts: list[str] = []
    if audio_mode == "original":
        # 素材原声经常比画面短，apad 补静音到片长，别让 -shortest 把画面截了
        audio_parts.append(
            f"[0:a]atrim=0:{total},apad=whole_dur={total},aresample=48000[aout]"
        )
    elif audio_mode == "mute":
        audio_parts.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{total}[aout]")
    elif audio_mode == "voice":
        mix_labels = []
        for index, sentence in enumerate(sentences):
            delay_ms = int(sentence["start"] * 1000)
            audio_parts.append(
                f"[{index + 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"adelay={delay_ms}|{delay_ms}[s{index}]"
            )
            mix_labels.append(f"[s{index}]")
        if use_music:
            music_label, music_chain = _music_label()
            audio_parts.append(music_chain)
            mix_labels.append(music_label)
        audio_parts.append(
            # apad 必须在 atrim 前面：口播最后一句结束得早，不补静音的话
            # 音轨会比画面短，-shortest 就把画面砍了（目标时长形同虚设）。
            # 加了 BGM 后口播+音乐可能顶满，alimiter 收一下别削波。
            "".join(mix_labels) + f"amix=inputs={len(mix_labels)}:normalize=0:duration=longest,"
            + ("alimiter=limit=0.95," if use_music else "")
            + f"apad=whole_dur={total},atrim=0:{total},asetpts=PTS-STARTPTS[aout]"
        )
    else:  # mix
        # 素材原声常常比成片短（口播把片子拉长了），mix 必须按最长算，
        # 再收口到总长：原声放完了剩下的时间留给口播，不能把口播切掉。
        audio_parts = [f"[0:a]atrim=0:{total},volume={bgm_volume},aresample=48000[bgm]"]
        mix_labels = ["[bgm]"]
        for index, sentence in enumerate(sentences):
            delay_ms = int(sentence["start"] * 1000)
            audio_parts.append(
                f"[{index + 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"adelay={delay_ms}|{delay_ms}[s{index}]"
            )
            mix_labels.append(f"[s{index}]")
        if use_music:
            music_label, music_chain = _music_label()
            audio_parts.append(music_chain)
            mix_labels.append(music_label)
        audio_parts.append(
            # apad 必须在 atrim 前面：口播最后一句结束得早，不补静音的话
            # 音轨会比画面短，-shortest 就把画面砍了（目标时长形同虚设）
            "".join(mix_labels) + f"amix=inputs={len(mix_labels)}:normalize=0:duration=longest,"
            + ("alimiter=limit=0.95," if use_music else "")
            + f"apad=whole_dur={total},atrim=0:{total},asetpts=PTS-STARTPTS[aout]"
        )

    filter_complex = ";".join(video_parts + audio_parts)

    out_path = Path(_windows_path(out_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{total}",
        "-shortest",     # 音轨比画面长时按画面收口，避免片尾留黑屏
        str(out_path),
    ]
    import os
    env = {**os.environ, **_fontconfig_env()}
    try:
        # cwd 指到字体目录：滤镜里的 ass/fontsdir 才能用相对路径（躲开盘符冒号）
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, env=env,
            cwd=str(DATA_RENDER_DIR),
        )
    finally:
        ass_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        raise RenderError("渲染失败：" + (detail[:900] or "未知错误"))
    return out_path


# ------------------------------------------------------------------ 后台任务

# 渲染任务状态（进程内）。渲染一条 30 秒片子约 1~2 分钟，
# 前端提交后轮询这里的 status。
RENDER_JOBS: dict[str, dict] = {}


def start_render_job(payload: dict) -> dict:
    """提交一条风格成片渲染，立即返回 render_id。payload 字段：

    template_id  必填，用哪个模板
    source       必填，素材视频路径（相对项目或绝对路径）
    storyboard   可选，直接给文案（[{text, label}]）；不给就按模板+facts 生成
    facts        可选，房源字段（area/partition/rent...），API 文案优先、规则兜底
    audio_mode   mix / original / voice / mute，默认 mix
    bgm_volume   原声音量，默认 0.30
    tts_voice / tts_rate  音色与语速
    kenburns     是否推拉运镜，默认开
    """
    import threading
    import uuid

    from .copywriting import generate_storyboard

    template_id = str(payload.get("template_id") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not template_id:
        raise RenderError("请指定 template_id")
    if not source:
        raise RenderError("请指定素材路径 source")
    audio_mode = str(payload.get("audio_mode") or "mix").strip().lower()
    if audio_mode not in AUDIO_MODES:
        raise RenderError(f"audio_mode 只能是 {'/'.join(sorted(AUDIO_MODES))}")

    render_id = uuid.uuid4().hex[:12]
    job: dict = {
        "id": render_id,
        "template_id": template_id,
        "status": "pending",
        "audio_mode": audio_mode,
        "output": "",
        "error": "",
        "created_at": utc_now(),
    }
    RENDER_JOBS[render_id] = job

    def run() -> None:
        try:
            from .db import connect

            job["status"] = "copywriting"
            with connect() as connection:
                row = connection.execute(
                    "SELECT spec_json FROM style_templates WHERE id=?", (template_id,)
                ).fetchone()
            if row is None:
                raise RenderError("模板不存在")
            spec = json.loads(row["spec_json"] or "{}")

            storyboard = payload.get("storyboard")
            if not storyboard:
                board, origin = generate_storyboard(spec, payload.get("facts") or {})
                job["copy_origin"] = origin
                storyboard = board

            job["status"] = "rendering"
            source_path = Path(_windows_path(source))
            if not source_path.is_absolute():
                source_path = BASE_DIR / source_path
            out = DATA_OUT_DIR / f"style-{render_id}.mp4"
            visual = _local_audio_visual_settings(render_id)
            render_storyboard(
                storyboard,
                source_path,
                out,
                bgm_volume=float(payload.get("bgm_volume") or 0.30),
                audio_mode=audio_mode,
                tts_voice=str(payload.get("tts_voice") or TTS_VOICE),
                tts_rate=str(payload.get("tts_rate") or TTS_RATE),
                kenburns=bool(payload.get("kenburns", True)),
                flower=visual["flower"],
                music_path=visual["music_path"],
                music_volume=visual["music_volume"],
            )
            job["status"] = "done"
            job["output"] = str(out)
        except Exception as error:  # noqa: BLE001 - 后台线程必须把失败写回状态
            job["status"] = "failed"
            job["error"] = str(error)[:500]

    threading.Thread(target=run, daemon=True).start()
    return job


DATA_OUT_DIR = BASE_DIR / "data" / "renders"


# ------------------------------------------------------------------ 任务本地出片

# 任务 voice 选项 → edge-tts 音色 + 语速。中介口播普遍偏快，慢了反而假；
# 但晓伊和云希本身语速就快，比晓晓少加一点
_VOICE_MAP = {
    "female_warm": (tts.EDGE_VOICES["female_warm"], "+10%"),
    "female_lively": (tts.EDGE_VOICES["female_lively"], "+8%"),
    "male_warm": (tts.EDGE_VOICES["male_warm"], "+8%"),
    "male_anchor": (tts.EDGE_VOICES["male_anchor"], "+10%"),
    "male_professional": (tts.EDGE_VOICES["male_professional"], "+6%"),
}


def _script_to_storyboard(script: str) -> list[dict]:
    """把任务口播稿拆成一镜一句。

    先按换行，再按句号问号切；单句太长（超过 24 字）再按逗号拆成两半，
    否则一屏字幕塞不下、画面也会一个镜头干等十几秒。
    """
    lines = [line.strip(" 　") for line in (script or "").splitlines()]
    if sum(1 for line in lines if line) < 2:
        pieces = re.split(r"[。！？!?\n]+", script or "")
        lines = [piece for piece in pieces if piece.strip()]

    storyboard: list[dict] = []
    for line in lines:
        text = line.strip(" 　，,")
        if not text:
            continue
        if len(text) <= 24:
            storyboard.append({"text": text})
            continue
        # 长句按逗号切成接近等长的两块，优先在中间附近断
        parts = [part for part in re.split(r"[，,；;]", text) if part.strip()]
        buffer = ""
        for part in parts:
            if buffer and len(buffer) + len(part) > 20:
                storyboard.append({"text": buffer})
                buffer = part
            else:
                buffer = f"{buffer}，{part}" if buffer else part
        if buffer:
            storyboard.append({"text": buffer})

    if not storyboard:
        raise RenderError("任务口播稿是空的，先在任务详情里生成文案")
    return storyboard


def _flag(settings: dict, key: str, default: str = "1") -> bool:
    value = str(settings.get(key) or default).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _task_value(task_row, key: str) -> str:
    """读任务级覆盖值。老库里没这列、或者填的是空串，都返回空让设置兜底。"""
    if task_row is None:
        return ""
    try:
        return str(task_row[key] or "").strip()
    except (IndexError, KeyError, TypeError):
        return ""


def _local_audio_visual_settings(task_key: str, task_row=None) -> dict:
    """读本机出片的观感参数，供 render_storyboard 用。

    对应工作台右侧那几组页签：花字 / 音乐 / 字幕 / 配音。值都做了兜底，
    老库里没有这些键也能正常出片。

    分两层：系统设置是默认值，任务上的 subtitle_size / subtitle_bottom /
    tts_rate / flower 是这条视频的例外。竖屏字号要更大、长文案语速要更快，
    这类差异逐条存在任务上，不值得让人每次先跑去设置里改一遍再改回来。
    """
    settings = get_settings()
    music_path = None
    if _flag(settings, "render_bgm"):
        mood = str(settings.get("render_bgm_mood") or "").strip() or None
        track = bgm.pick(task_key, mood)
        if track:
            music_path = track["path"]
    try:
        volume = float(settings.get("render_bgm_volume") or 0.14)
    except ValueError:
        volume = 0.14

    size_key = _task_value(task_row, "subtitle_size") or str(
        settings.get("render_subtitle_size") or "medium"
    ).strip()
    bottom_key = _task_value(task_row, "subtitle_bottom") or str(
        settings.get("render_subtitle_bottom") or "middle"
    ).strip()
    density_key = str(settings.get("render_flower_density") or "normal").strip()
    flower_style = str(settings.get("render_flower_style") or "auto").strip() or "auto"
    source = str(settings.get("render_highlight_source") or "ai").strip()

    # 花字开关：任务上写了 0/1 就听任务的，否则听设置
    task_flower = _task_value(task_row, "flower")
    if task_flower == "1":
        flower_on = True
    elif task_flower == "0":
        flower_on = False
    else:
        flower_on = _flag(settings, "render_flower")

    return {
        "flower": flower_on,
        "flower_density": FLOWER_DENSITY_CHOICES.get(density_key, 3),
        "flower_style": flower_style,
        "music_path": music_path,
        "music_volume": max(0.0, min(volume, 0.5)),
        "caption_size": CAPTION_SIZE_CHOICES.get(size_key, CAPTION_SIZE),
        "caption_bottom": CAPTION_BOTTOM_CHOICES.get(bottom_key, CAPTION_MARGIN_V),
        "caption_highlight": _flag(settings, "render_subtitle_highlight"),
        "highlight_source": source if source in {"ai", "rules", "manual"} else "ai",
        "kenburns": _flag(settings, "render_kenburns"),
        "tts_rate": _task_value(task_row, "tts_rate") or str(settings.get("render_tts_rate") or "").strip(),
        # 正则兜底：只有「手动【】」不需要
        "caption_highlight_regex": source != "manual",
    }


def render_task_locally(task_row) -> Path:
    """不走视频工作台，直接在本机用 ffmpeg 出片。

    给「工作台授权根目录都不在本机」的场景兜底：素材用任务原始视频，
    口播用任务文案逐句 TTS，画面智能选镜 + Ken Burns，原声默认压低铺底。
    """
    from .db import EXPORT_DIR
    from .hygiene import clean_internal_sentences

    settings = get_settings()
    script, _removed = clean_internal_sentences(task_row["script"])
    storyboard = _script_to_storyboard(script)

    source = Path(_windows_path(task_row["source_video_path"]))
    if not source.is_file():
        raise RenderError(f"任务原始视频不存在：{source}")

    voice, rate = _VOICE_MAP.get(str(task_row["voice"] or ""), _VOICE_MAP["female_warm"])
    # 这条任务自己选过原声就听它的，没选才回到设置里的默认值
    task_audio = ""
    try:
        task_audio = str(task_row["audio_mode"] or "").strip().lower()
    except (IndexError, KeyError):
        task_audio = ""
    audio_mode = task_audio if task_audio in AUDIO_MODES else str(settings.get("local_audio_mode") or "mix").strip().lower()
    if audio_mode not in AUDIO_MODES:
        audio_mode = "mix"
    canvas = (CANVAS_WIDE_W, CANVAS_WIDE_H) if task_row["aspect_ratio"] == "16:9" else (CANVAS_W, CANVAS_H)

    visual = _local_audio_visual_settings(str(task_row["task_no"]), task_row)
    # 语速：任务上选了听任务的，否则听设置里那个，再否则用音色自带
    if visual.get("tts_rate"):
        rate = visual["tts_rate"]

    # AI 挑字幕高亮词：没配 key、调用失败都静默回退正则，不挡出片。
    # 设置里选了「规则」或「只用【】」就别白花一次模型调用。
    if visual["highlight_source"] == "ai":
        try:
            from .llm import pick_highlights
            picks = pick_highlights([item["text"] for item in storyboard])
            for item, words in zip(storyboard, picks):
                if words:
                    item["highlights"] = words
        except Exception as error:  # noqa: BLE001 - 高亮词是锦上添花
            logger.info("AI 高亮词跳过：%s", error)

    out_dir = EXPORT_DIR / str(task_row["task_no"])
    out = out_dir / "local.mp4"
    # target_duration = 0 表示「根据原素材」：用素材视频流的真实长度当目标
    goal = float(task_row["target_duration"] or 0)
    if goal <= 0:
        try:
            goal = video_duration(source)
        except RenderError:
            goal = 0.0
    started = time.time()
    render_storyboard(
        storyboard,
        source,
        out,
        audio_mode=audio_mode,
        tts_voice=voice,
        tts_voice_key=str(task_row["voice"] or "female_warm"),
        tts_rate=rate,
        canvas=canvas,
        goal_seconds=goal or None,
        flower=visual["flower"],
        flower_density=visual["flower_density"],
        flower_style=visual["flower_style"],
        music_path=visual["music_path"],
        music_volume=visual["music_volume"],
        caption_size=visual["caption_size"],
        caption_bottom=visual["caption_bottom"],
        caption_highlight=visual["caption_highlight"],
        caption_highlight_regex=visual["caption_highlight_regex"],
        kenburns=visual["kenburns"],
    )
    logger.info("本地出片完成：%s（原声=%s，%.1fs）", out, audio_mode, time.time() - started)

    # 用户自己设定的出品目录：成片再复制一份过去，方便直接取用
    export_root = str(settings.get("export_output_root") or "").strip()
    if export_root:
        target_dir = Path(export_root)
        if not target_dir.is_absolute():
            target_dir = (BASE_DIR / target_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, target_dir / f"{task_row['task_no']}.mp4")
        logger.info("成片已复制到出品目录：%s", target_dir)
    return out
