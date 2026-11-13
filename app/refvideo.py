"""本地拆解参考视频：不联网、不调云端模型，把一条片子读成可复用的模板。

拆解看三件事：

1. **节奏** —— ffmpeg 的场景变化找出切点，算出平均镜头长度；
   一镜到底的片子没有切点，按时间等分出占位段落（标 synthetic）交给人工划段；
2. **字幕** —— 剪映导出的片子字幕烧在画面里，用 OCR 按帧读，
   顺带把「顶部功能区角标」和「底部口播字幕」分开（位置差得很远，很好分）；
3. **结构** —— 按句子角色（钩子 / 面积 / 亮点 / 收尾）归类，算出每段占多少时长。

**口播是可选件**：`transcribe()` 还留着，但自拍素材是空镜（旁白后期配），
装语音模型是空转。只在拆「别人的、没有字幕、但有真人说话」的片子时才用得上。

产出的 spec 和云端拆解保持同一套字段，上层套用时不用区分来源。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .asr import AsrError, _windows_path, probe_capabilities, read_captions, transcribe
from .db import json_dumps, transaction, utc_now
from .shots import ShotError, _find_binary, probe_duration

# 一句话的舒适区间：太短是碎片，太长一屏放不下
MAX_WORDS = 22
_CHINESE = 4.5  # 中文口播大约每秒 4~5 个字

_CTA = re.compile(r"感兴趣|欢迎|过来看|私信|联系|留言|评论区|点赞|关注|咨询|带看")
_AREA = re.compile(r"\d+\s*(?:平|㎡|平方米)")
_ZONE = re.compile(r"老板|工位|工区|会议室|前台|接待|入户|茶水|阳台|仓|店|铺|隔间|间")
_HOOK = re.compile(r"今天|带大家|推荐|看看|找到|带你|一套")


class RefVideoError(RuntimeError):
    pass


# ------------------------------------------------------------------ 画面

def scene_cuts(path: Path, threshold: float = 0.28) -> list[float]:
    """返回切点时间（秒）。阈值沿用了项目里人工验证过的经验值。"""
    ffmpeg = _find_binary("ffmpeg")
    completed = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-nostats", "-i", _windows_path(path),
            "-filter:v", f"select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-",
        ],
        capture_output=True,
        check=False,
    )
    text = completed.stderr.decode("utf-8", "ignore")
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", text)]
    # 相邻两个切点几乎重合时只留一个（同一帧被判定两次）
    deduped: list[float] = []
    for value in times:
        if not deduped or value - deduped[-1] > 0.25:
            deduped.append(value)
    return deduped


def _grab_frames(path: Path, fps: float = 1.0) -> list[tuple[float, str]]:
    """按固定帧率抽帧，写进临时目录，返回 [(时间, 路径)]。"""
    ffmpeg = _find_binary("ffmpeg")
    folder = Path(tempfile.mkdtemp(prefix="ova-ref-frames-"))
    completed = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", _windows_path(path),
            "-vf", f"fps={fps}", "-q:v", "3", str(folder / "%05d.jpg"),
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "ignore").strip()[:300]
        raise RefVideoError(f"抽帧失败：{message or '未知错误'}")
    frames = []
    for index, file in enumerate(sorted(folder.glob("*.jpg")), start=0):
        frames.append((index / fps, str(file)))
    return frames


def _split_captions(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """把 OCR 结果分成「顶部角标」和「底部字幕」。

    中介视频的版式很固定：功能区名字在画面上方（约占 20~30% 高度），
    口播字幕压在下方（约占 70%）。按 y 坐标一刀切开就够，不需要版面模型。
    """
    labels = [r for r in rows if r.get("y", 1.0) < 0.45]
    subtitles = [r for r in rows if r.get("y", 1.0) >= 0.45]
    return labels, subtitles


def _compact(text: str) -> str:
    return re.sub(r"\s", "", text or "")


def _pick_subtitle_band(
    rows: list[dict], *, min_segments: int = 4, max_gap: float = 2.5, min_y: float = 0.55
) -> list[dict]:
    """从 OCR 结果里挑出真正是字幕的那一条带。

    只看 y 坐标不行：实测一条 25 秒视频，画面下方 87 段「文字」里 85 段是包装上的字。
    试过两个判据都不成立——按「重复度」挑会选中包装上反复出现的那行字，
    按「文本多变」挑会选中背面特写里被 OCR 读花的说明字。

    真正成立的是**时间连续性**：口播字幕是一条不断的流，每 1~2 秒换一句，
    从片头铺到片尾，中间不会有长空白；包装、门头、易拉宝上的字只在某个镜头
    出现一次，前后都是大段空白。所以按 y 分桶后，
    只保留「段数够多、中间不断流、位置在下半屏」的一桶。
    """
    if not rows:
        return []

    buckets: dict[int, list[dict]] = {}
    for row in rows:
        text = _compact(row.get("text", ""))
        if not text or float(row.get("y", 1.0)) < min_y:
            continue
        buckets.setdefault(round(float(row.get("y", 1.0)) / 0.04), []).append(row)

    best: tuple[int, list[dict]] | None = None
    for items in buckets.values():
        items = sorted(items, key=lambda row: row["start"])
        if len(items) < min_segments:
            continue
        gap = max(
            (later["start"] - earlier["end"] for earlier, later in zip(items, items[1:])),
            default=0.0,
        )
        if gap > max_gap:
            continue
        if len({_compact(row["text"]) for row in items}) < 3:
            continue
        if best is None or len(items) > best[0]:
            best = (len(items), items)

    return best[1] if best else []


def _covers_enough(sentences: list[dict], duration: float) -> bool:
    """字幕要贯穿全片才算拿到了台词，零星几行说明认错了东西。"""
    if len(sentences) < 2:
        return False
    spoken = sum(row["end"] - row["start"] for row in sentences)
    return spoken >= duration * 0.25


def _similar(left: str, right: str) -> float:
    from difflib import SequenceMatcher

    a, b = _compact(left), _compact(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _coalesce(rows: list[dict], gap: float = 1.0, likeness: float = 0.5) -> list[dict]:
    """把同一条字幕的连续几帧并成一句。

    OCR 是每秒抽 1.2 帧认的，一句字幕会连着出现两三帧，而且每帧读出来的字
    略有出入（「怎么分辨」/「怎么分瓣」）。

    只按时间间隙合并会翻车：抽帧间隔是 0.83 秒，同句和相邻句的间隔一样长，
    一路并下去整条片子只剩两句。所以必须再加一道文本相似度——
    同一句的不同读法相似度在 0.7 以上，换句之后会掉到 0.3 以下。
    """
    merged: list[dict] = []
    for row in sorted(rows, key=lambda item: item["start"]):
        if (
            merged
            and row["start"] - merged[-1]["end"] <= gap
            and _similar(row["text"], merged[-1]["text"]) >= likeness
        ):
            last = merged[-1]
            last["end"] = max(last["end"], row["end"])
            # 同一句被读成好几个版本时，取字最多的那版——漏字比多字更常见
            if len(_compact(row["text"])) > len(_compact(last["text"])):
                last["text"] = row["text"]
            continue
        merged.append(dict(row))
    return merged


# ------------------------------------------------------------------ 结构

def classify(text: str, index: int, total: int) -> str:
    """给一句话定角色：钩子 / 面积 / 亮点 / 收尾。"""
    if _CTA.search(text):
        return "cta"
    if index == 0 and (_HOOK.search(text) or _AREA.search(text)):
        return "hook"
    if _AREA.search(text):
        return "面积"
    if index == total - 1:
        return "cta"
    if _ZONE.search(text):
        return "亮点"
    return "亮点"


# 只有画面、没有台词时的骨架：开头抓人、中间讲房、最后收口。
# 顺序和 copywriting._LINE_BANK 的话题名对齐，这样套模板时能直接落到句式库。
_SKELETON = ("hook", "area", "zone", "furn", "boss", "value", "cta")


def skeleton_roles(count: int) -> list[str]:
    """镜头数 → 角色序列。第一镜钩子、最后一镜收尾，中间按看房顺序循环。"""
    if count <= 0:
        return []
    if count == 1:
        return ["hook"]
    middle = [role for role in _SKELETON[1:-1]]
    roles = ["hook"]
    for index in range(count - 2):
        roles.append(middle[index % len(middle)])
    roles.append("cta")
    return roles


def _merge_close(bounds: list[float], min_seconds: float = 0.6) -> list[float]:
    """切点太密会把一个摇镜拆成三段，合并成一段更接近人眼感知的镜头。"""
    merged: list[float] = []
    for value in bounds:
        if not merged or value - merged[-1] >= min_seconds:
            merged.append(value)
    return merged


def audio_mean_db(path: Path) -> float | None:
    """音轨的平均电平（dBFS），用来判断这条片子有没有人声。

    不分辨人声和背景音乐——那要频谱模型，不值当。只报数字让人自己判断：
    空镜实拍素材通常在 -40 以下，正常说话在 -25 以上，纯 BGM 介于中间。
    """
    ffmpeg = _find_binary("ffmpeg")
    if not ffmpeg:
        return None
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", _windows_path(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", completed.stderr or "")
    return float(match.group(1)) if match else None


def _shots_from_cuts(duration: float, cuts: list[float]) -> list[dict]:
    """没有台词时，用画面切点当镜头边界。

    这不是退而求其次的猜测：镜头数是这条片子最硬的节奏信息，
    几秒一换镜、开场留多久，都能直接拿去指导拍摄。

    一镜到底的片子（实拍走位带看最常见）没有切点，整片会变成一个镜头，
    拆出来对拍摄毫无用处。这种情况按时间等分出「段落格子」——注意是等分，
    不是猜边界：一镜里的空间段落只能靠看画面判断，脚本猜不准。
    实测过用直方图帧差找边界，一条 10 秒走位片切出 21 个峰，
    全落在镜头扫过窗户的瞬间（帧差天然大），全是假切点。
    """
    edges = [0.0] + [c for c in _merge_close(cuts) if 0.2 < c < duration - 0.2] + [duration]
    spans = [(a, b) for a, b in zip(edges, edges[1:]) if b - a > 0.3] or [(0.0, duration)]

    synthetic = False
    if len(spans) == 1 and duration >= 6:
        count = max(2, min(int(round(duration / 2.5)), 8))
        step = duration / count
        spans = [(index * step, (index + 1) * step) for index in range(count)]
        # 末段吸附到真实时长，否则逐段取整后总和对不上，结构占比会算出 100.9%
        spans[-1] = (spans[-1][0], duration)
        synthetic = True

    roles = skeleton_roles(len(spans))
    shots = []
    for index, (start, end) in enumerate(spans):
        shot = {
            "index": index + 1,
            "role": roles[index],
            "start": round(start, 2),
            "end": round(end, 2),
            "seconds": round(end - start, 1),
            "sample": "",
            "words": 0,
        }
        if synthetic:
            # 标出来：这段边界是等分的，不是真的换镜，复核时必须人工改
            shot["synthetic"] = True
        shots.append(shot)
    return shots


def build_spec(
    duration: float,
    cuts: list[float],
    sentences: list[dict],
    labels: list[dict],
) -> dict:
    """把节奏、口播、角标合成一份模板 spec。

    台词不是必需品：没读到台词时只产出「镜头数与节奏」，角色按看房骨架补。
    这样即使本机没装语音模型，拆解仍然能给出可用结论。
    """
    if not duration:
        raise RefVideoError("视频时长为 0，文件可能损坏")

    if sentences:
        shots = []
        total = len(sentences)
        for index, row in enumerate(sentences):
            text = row["text"].strip()
            seconds = round(max(row["end"] - row["start"], 0.4), 1)
            shots.append({
                "index": index + 1,
                "role": classify(text, index, total),
                "start": row["start"],
                "end": row["end"],
                "seconds": seconds,
                "sample": text,
                "words": len(text),
            })
    else:
        shots = _shots_from_cuts(duration, cuts)

    # 每个镜头的画面里出现了哪些角标——套用时要按同样的位置放角标
    for shot in shots:
        inside = [
            label["text"] for label in labels
            if shot["start"] - 0.5 <= label["start"] <= shot["end"] + 0.5
        ]
        if inside:
            shot["labels"] = list(dict.fromkeys(inside))

    # 结构占比：同类角色合并
    shares: dict[str, float] = {}
    for shot in shots:
        shares[shot["role"]] = round(shares.get(shot["role"], 0) + shot["seconds"], 1)
    structure = [
        {"role": role, "share": round(seconds / duration, 3), "seconds": seconds}
        for role, seconds in sorted(shares.items(), key=lambda item: -item[1])
    ]

    words = sum(shot["words"] for shot in shots)
    cut_span = cuts or []
    avg_shot = round(duration / max(len(shots), 1), 1)
    longest = max(shots, key=lambda s: s["seconds"])
    pacing = {
        "cut_count": len(cut_span),
        "cuts": [round(value, 2) for value in cut_span],
        "avg_cut_seconds": round(duration / (len(cut_span) + 1), 2) if duration else 0,
        "sentence_count": len(shots),
        "words_per_second": round(words / duration, 2) if duration else 0,
    }

    last = shots[-1]
    has_text = any(shot["words"] for shot in shots)
    synthetic = any(shot.get("synthetic") for shot in shots)

    tips: list[str] = []
    if has_text:
        tips.append(f"全长 {duration:.0f} 秒，共 {len(shots)} 句，平均一句 {avg_shot} 秒")
    elif synthetic:
        tips.append(f"全长 {duration:.0f} 秒，一镜到底，按时间等分成 {len(shots)} 段")
        tips.append("没有换镜切点，这 %d 段只是占位：真正走了几个空间、每段讲什么，要看拼图确认" % len(shots))
    else:
        tips.append(f"全长 {duration:.0f} 秒，共 {len(shots)} 个镜头，平均 {avg_shot} 秒一换")
    if cut_span:
        tips.append(f"画面切点 {len(cut_span)} 个，平均每 {duration / (len(cut_span) + 1):.1f} 秒换一次镜")
    if has_text:
        tips.append(f"语速约 {words / duration:.1f} 字/秒，最长的一句 {longest['seconds']} 秒（第 {longest['index']} 句）")
    else:
        tips.append("没读到台词：只拆了画面节奏，套模板时文案由本地句式库按这套镜头数生成")
    if labels:
        tips.append(f"画面上方有功能区角标 {len(labels)} 处，是这套模板的识别度来源")
    if len(shots) <= 2 and cut_span:
        # 纯卡点片：没有口播，卖点全靠音乐切换和字幕点题
        tips.append(f"这是卡点片：{len(cut_span) + 1} 个镜头跟着音乐切，台词极少，靠画面节奏和字幕点题")
    if has_text and last["role"] == "cta":
        tips.append(f"收尾第 {last['index']} 句是明确的行动号召：" + last["sample"])

    if not has_text and synthetic:
        summary = f"一镜到底、{duration:.0f} 秒，按时间等分出 {len(shots)} 段待复核"
    elif not has_text:
        summary = f"{len(shots)} 个镜头、{duration:.0f} 秒，平均 {avg_shot} 秒一换，台词没读到"
    elif len(shots) <= 2 and cut_span:
        summary = f"卡点片：{len(cut_span) + 1} 个镜头、{duration:.0f} 秒，台词只有 {len(shots)} 句"
    else:
        summary = f"{len(shots)} 句口播、{len(cut_span)} 个切点，{duration:.0f} 秒讲完一套房源"

    return {
        "engine": "local",
        "text_source": "asr" if has_text else "none",
        "summary": summary,
        "tone": "口语、短句、第二人称带看口吻" if _HOOK.search(shots[0]["sample"]) else "口语直述",
        "structure": structure,
        "pacing": pacing,
        "shots": shots,
        "labels": labels,
        "constraints": {
            "max_words_per_shot": MAX_WORDS,
            "total_seconds": round(duration, 1),
            "words_per_second": round(words / duration, 2),
        },
        "tips": tips,
    }


# ------------------------------------------------------------------ 入口

def _fuse(subtitles: list[dict], sentences: list[dict]) -> list[dict]:
    """以转写的切句为准，用画面字幕校正措辞。

    转写听得出句子边界，但会把「220平」听成「220瓶」、把「茶几」听成「插起」；
    画面字幕是设计稿，字不会错，但每半秒才抽一帧，边界是碎的。
    所以每条字幕只归给时间重叠最多的那一句——不能贪心取窗口，
    否则相邻两句会互相抢字，出现「推荐一套220平」粘在第二句开头这种事。
    """
    if not subtitles:
        return sentences

    buckets: dict[int, list[dict]] = {id(row): [] for row in sentences}
    for sub in subtitles:
        target, best = None, 0.0
        for row in sentences:
            overlap = min(sub["end"], row["end"]) - max(sub["start"], row["start"])
            if overlap > best:
                target, best = row, overlap
        if target is not None:
            buckets[id(target)].append(sub)

    for row in sentences:
        assigned = sorted(buckets[id(row)], key=lambda s: s["start"])
        if not assigned:
            continue
        pieces: list[str] = []
        for sub in assigned:
            text = sub["text"]
            if any(text in kept for kept in pieces):   # 「应俱全」已被「一应俱全」包含
                continue
            pieces = [kept for kept in pieces if kept not in text]
            pieces.append(text)
        fused = "".join(pieces)
        if len(re.sub(r"\s", "", fused)) >= 2:
            row["spoken"] = row["text"]
            row["text"] = fused

    # 转写整段漏掉的地方，用那段字幕补回来，避免内容凭空消失
    filled: list[dict] = []
    for row in sentences:
        if filled and row["start"] - filled[-1]["end"] > 4.0:
            gap_start, gap_end = filled[-1]["end"], row["start"]
            orphans = [s for s in subtitles if gap_start < s["start"] < gap_end]
            if orphans:
                filled.append({
                    "start": round(orphans[0]["start"], 2),
                    "end": round(orphans[-1]["end"] + 0.5, 2),
                    "text": "".join(dict.fromkeys(s["text"] for s in orphans)),
                })
        filled.append(row)
    return filled


def analyze_file(path: Path) -> dict:
    """完整拆一条片子：时长 → 切点 → 转写 → 字幕 → 合成。

    台词是加分项，不是前置条件：听不到就只交节奏，并把原因写进 warnings，
    界面上能直接告诉使用者「还差什么」，而不是弹一个看不懂的报错。
    """
    path = Path(_windows_path(path))
    if not path.is_file():
        raise RefVideoError(f"参考视频不存在：{path}")

    caps = probe_capabilities()
    warnings: list[str] = []

    try:
        duration = probe_duration(path)
        cuts = scene_cuts(path)
    except ShotError as error:
        raise RefVideoError(str(error)) from error

    sentences: list[dict] = []
    text_source = "none"
    if caps["speech_ready"]:
        try:
            sentences = transcribe(path)
            text_source = "asr"
        except AsrError as error:
            warnings.append(f"口播转写失败：{error}｜这次只拆画面节奏")
    else:
        # 别一上来就说"你没装模型"：中介自己拍的素材本来就是空镜，
        # 旁白是后期配的，从来没有真人说话这回事，报缺件只会让人以为哪坏了
        level = audio_mean_db(path)
        if level is not None and level < -35:
            warnings.append(
                f"这条片子音轨基本是静音（平均 {level:.0f}dB），本来就没有口播；"
                "空镜素材走「脚本生成文案 → 配音」这条路，不需要听人说话"
            )
        else:
            warnings.append("本机没装语音模型，这次只拆画面节奏；想要逐句台词，见设置里的语音模型说明")

    labels: list[dict] = []
    if caps["caption_ocr_ready"]:
        try:
            frames = _grab_frames(path, fps=1.2)
            rows = read_captions(frames)
            labels, subtitles = _split_captions(rows)
            subtitles = _coalesce(_pick_subtitle_band(subtitles))
            if not _covers_enough(subtitles, duration):
                subtitles = []
            if sentences:
                # 口播与字幕都有：时间用转写，文字用字幕
                sentences = _fuse(subtitles, sentences)
            elif subtitles:
                # 纯字幕片（只有 BGM）：底部字幕就是台词
                sentences = subtitles
                text_source = "caption"
        except Exception as error:  # noqa: BLE001 - OCR 是增益，失败不该毁掉整条分析
            labels = []
            warnings.append(f"字幕识别失败：{str(error)[:120]}")

    spec = build_spec(duration, cuts, sentences, labels)
    spec["duration"] = round(duration, 1)
    spec["cuts"] = cuts
    spec["text_source"] = text_source
    if any(shot.get("synthetic") for shot in spec.get("shots") or []):
        warnings.append(
            "这条片子没有任何换镜切点（一镜到底），镜头边界是按时间等分的占位，"
            "必须打开拼图人工划分空间段落，否则结构占比是假的"
        )
    spec["warnings"] = warnings
    return spec


def analyze_template(template_id: str) -> dict:
    """后台线程入口：拆解并写回 style_templates。"""
    with transaction() as connection:
        row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise RefVideoError("模板不存在")

    try:
        spec = analyze_file(Path(row["source_path"]))
        transcript = "\n".join(shot["sample"] for shot in spec["shots"])
        with transaction() as connection:
            connection.execute(
                """UPDATE style_templates SET status='ready', spec_json=?, transcript=?,
                   duration_s=?, error='', updated_at=? WHERE id=?""",
                (json_dumps(spec), transcript, spec["duration"], utc_now(), template_id),
            )
        return spec
    except Exception as error:  # noqa: BLE001 - 失败必须写回库，界面才知道
        with transaction() as connection:
            connection.execute(
                "UPDATE style_templates SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(error)[:500], utc_now(), template_id),
            )
        raise
