"""智能选镜：从素材里自动挑出干净、好看、能当空镜用的片段。

以前是硬编码切点（只对那一条测试素材有效），现在交给本地量化模型做三件事：

1. **场景切点**（ffmpeg scene 检测）把素材切成候选段；
2. **OpenCV 画质评分**：拉普拉斯方差（清晰度）、平均亮度（避过暗/过曝）、
   饱和度（避灰蒙蒙）。三项归一后加权，先给所有候选段排个序；
3. **OCR + 人形检测**只对「分数最高的那几段」做：查烧死的字幕/角标，
   Haar 查有人物出镜。全段都跑 OCR 太贵（一段素材要一分钟），
   而真正会被选中的只有前几名，只查它们就够。

结果按「文件路径 + 修改时间 + 大小」缓存到 data/cache，同一素材重复出片
不用再算一遍。

打分不看「美不美」这种主观题，只排除硬伤：有字、糊、黑、死白、有人。
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from pathlib import Path

from .asr import _windows_path, ocr_ready
from .refvideo import scene_cuts
from .shots import _find_binary, probe_duration

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_PATH = BASE_DIR / "data" / "cache" / "shot_select.json"
_CACHE_LOCK = threading.Lock()

# 一段最低 1.4 秒、最高 3 秒——切细一点候选才够多，
# 一条片子十来句口播时不至于反复用同一段
MIN_CLIP = 1.4
MAX_CLIP = 3.0
# 每段最多取几帧评分
FRAMES_PER_CLIP = 3
# 抽帧密度（帧/秒）。0.5 秒一帧足够判断清晰度和亮度，OCR 也能用
SAMPLE_FPS = 2.0
# 抽帧时先缩到 480 宽：评分和 OCR 都够用，读写快一个数量级
SAMPLE_WIDTH = 480
# 只对分数最高的前 N 段做 OCR/人形检测（真正会被选中的就是这几段）
OCR_TOP = 12
# 抽帧里出现烧死字幕的占比超过这个值就整段排除
TEXT_FRAME_RATIO = 0.2
# 检测到人物（人脸/上半身）的帧占比超过这个值就排除
PERSON_FRAME_RATIO = 0.2
# 整段选镜最多花多久，超时就退回均匀切段，不能让出片卡死
SELECT_BUDGET = 120.0

WEIGHTS = {"sharpness": 0.45, "brightness": 0.30, "color": 0.25}

# 选镜结果的结构变过一次就加一，老缓存自动作废
CACHE_VERSION = 3


class ShotSelectError(RuntimeError):
    pass


def _ffmpeg() -> str:
    try:
        return _find_binary("ffmpeg")
    except Exception as error:  # noqa: BLE001
        raise ShotSelectError(str(error)) from error


# ------------------------------------------------------------------ 画面评分

def _frame_scores(image_path: str) -> dict[str, float] | None:
    """单帧三项评分，全部归一到 0~1。"""
    import cv2
    import numpy as np  # noqa: F401 - cv2 依赖 numpy，显式导入避免环境差异

    image = cv2.imread(image_path)
    if image is None:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # 清晰度：拉普拉斯方差。室内手机实拍 60 以下算糊，600 以上很锐。
    laplacian = cv2.Laplacian(gray, cv2.CV_64F).var()
    sharpness = min(laplacian / 400.0, 1.0)

    # 亮度：理想区间 90~170（0~255），偏离越远越扣
    mean = float(gray.mean())
    if mean < 60:
        brightness = 0.0
    elif mean < 90:
        brightness = (mean - 60) / 30
    elif mean <= 170:
        brightness = 1.0
    elif mean <= 215:
        brightness = (215 - mean) / 45
    else:
        brightness = 0.0

    # 饱和度：灰蒙蒙的片子 HSV 的 S 均值很低
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    sat = float(hsv[:, :, 1].mean())
    color = min(max((sat - 12) / 60.0, 0.0), 1.0)

    # 信息量：Canny 边缘占比。家具、隔断、窗框、踢脚线越多越高；
    # 白墙和空走廊接近 0。实测室内实拍落在 3%~10%，所以 14% 封顶——
    # 除数设小了（比如 6%）所有镜头都会顶到 1.0，等于白算。
    # 它不进 WEIGHTS：只用来挑开场镜头，掺进总分会把既有排序全打乱。
    edges = cv2.Canny(gray, 60, 160)
    detail = min(float((edges > 0).mean()) / 0.14, 1.0)

    return {"sharpness": sharpness, "brightness": brightness, "color": color, "detail": detail}


def _score_frames(paths: list[str]) -> tuple[float, float]:
    """返回 (画质总分, 画面信息量)。两者分开用：总分决定用不用这段，
    信息量只决定谁当开场镜头。"""
    import numpy as np

    per_frame = [score for p in paths if (score := _frame_scores(p))]
    if not per_frame:
        return 0.0, 0.0
    means = {key: float(np.mean([frame[key] for frame in per_frame])) for key in WEIGHTS}
    total = sum(WEIGHTS[key] * means[key] for key in WEIGHTS)
    detail = float(np.mean([frame.get("detail", 0.0) for frame in per_frame]))
    return total, detail


# ------------------------------------------------------------------ 文字 / 人物

def _text_frame_ratio(image_paths: list[str]) -> float:
    """抽帧 OCR，返回出现字幕/角标文字的帧占比。

    只看两条文字带（跟拆解参考视频同一套逻辑），窗外的楼牌不算。
    """
    from .asr import _ocr_frame

    hits = 0
    for path in image_paths:
        if _ocr_frame((0.0, path)):
            hits += 1
    return hits / max(len(image_paths), 1)


def _person_ratio(image_paths: list[str]) -> float:
    """抽帧检测人物（Haar 级联：正面脸 + 上半身），返回命中帧占比。"""
    import cv2

    cascades = []
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_upperbody.xml"):
        try:
            cascade = cv2.CascadeClassifier(str(cv2.data.haarcascades / name))
            if not cascade.empty():
                cascades.append(cascade)
        except Exception:  # noqa: BLE001 - 模型缺失就跳过这项检查
            continue
    if not cascades:
        return 0.0

    hits = 0
    for path in image_paths:
        image = cv2.imread(path)
        if image is None:
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        if any(len(cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)))
               for cascade in cascades):
            hits += 1
    return hits / max(len(image_paths), 1)


# ------------------------------------------------------------------ 抽帧

def _sample_frames(path: Path, folder: Path) -> tuple[list[tuple[float, str]], float]:
    """一个 ffmpeg 进程把全片按 SAMPLE_FPS 抽成小图，返回 ([(时间, 路径)], 时长)。"""
    import subprocess

    duration = probe_duration(path)
    fps = SAMPLE_FPS if duration <= 180 else 1.0   # 长素材降密度，别抽几千帧
    completed = subprocess.run(
        [
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", _windows_path(path),
            "-vf", f"fps={fps},scale={SAMPLE_WIDTH}:-2",
            "-q:v", "4", str(folder / "%05d.jpg"),
        ],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise ShotSelectError(f"抽帧失败：{(completed.stderr or '').strip()[:300]}")
    frames = []
    for index, file in enumerate(sorted(folder.glob("*.jpg"))):
        frames.append((round((index + 0.5) / fps, 3), str(file)))
    return frames, duration


# ------------------------------------------------------------------ 候选段

def _split_segments(cuts: list[float], duration: float) -> list[tuple[float, float]]:
    """按切点切候选段：过长切短、过短并入相邻段。"""
    bounds = [0.0] + [value for value in cuts if 0 < value < duration] + [duration]
    raw = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]

    segments: list[tuple[float, float]] = []
    for start, end in raw:
        cursor = start
        while end - cursor > MAX_CLIP:
            segments.append((cursor, cursor + MAX_CLIP))
            cursor += MAX_CLIP
        remain = end - cursor
        if remain >= MIN_CLIP:
            segments.append((cursor, end))
        elif segments and (end - segments[-1][0]) <= MAX_CLIP:
            segments[-1] = (segments[-1][0], end)
        elif remain > 0.4:
            segments.append((cursor, end))
    return segments


def _cache_key(path: Path, stat) -> str:
    # CACHE_VERSION 进 key：改了打分口径（比如加了 detail），老缓存必须失效，
    # 否则同一段素材永远用旧分数，改动看上去"没生效"。
    return f"{path.resolve()}|{int(stat.st_mtime)}|{stat.st_size}|{MAX_CLIP}|{CACHE_VERSION}"


def _load_cache(key: str) -> list[dict] | None:
    try:
        if not CACHE_PATH.is_file():
            return None
        with _CACHE_LOCK:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data.get(key)
    except Exception:  # noqa: BLE001 - 缓存坏了就当没有
        return None


def _save_cache(key: str, clips: list[dict]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _CACHE_LOCK:
            data = {}
            if CACHE_PATH.is_file():
                try:
                    data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                except ValueError:
                    data = {}
            data[key] = clips
            # 只留最近 60 条，文件不会无限长大
            if len(data) > 60:
                for old_key in list(data)[: len(data) - 60]:
                    data.pop(old_key, None)
            CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as error:  # noqa: BLE001 - 缓存写失败不影响出片
        logger.debug("选镜缓存写入失败：%s", error)


def candidate_clips(path: str | Path, *, ocr_top: int = OCR_TOP, prefer_clean: bool = True) -> list[dict]:
    """把素材切成候选段并评分，按分数从高到低返回。

    每项 {start, end, seconds, score, has_text}。has_text=True 表示
    带烧死的字幕或有人物出镜，选镜时垫底。
    """
    started = time.time()
    path = Path(_windows_path(path))
    if not path.is_file():
        raise ShotSelectError(f"素材不存在：{path}")

    stat = path.stat()
    key = _cache_key(path, stat)
    cached = _load_cache(key)
    if cached:
        logger.info("选镜命中缓存：%d 段", len(cached))
        return cached

    work = Path(tempfile.mkdtemp(prefix="ova-select-"))
    try:
        frames, duration = _sample_frames(path, work)
        cuts = scene_cuts(path)
        segments = _split_segments(cuts, duration)
    except Exception as error:  # noqa: BLE001 - 探测失败退回整片均分
        logger.warning("选镜探测失败（%s），退回均分切段", str(error)[:120])
        duration = probe_duration(path)
        segments = [(i * 3.0, min(duration, (i + 1) * 3.0))
                    for i in range(max(1, int(duration // 3.0)))]
        frames = []

    # 1) 先给所有段打画质分（纯 CV，快）
    scored: list[dict] = []
    for start, end in segments:
        inside = [item for item in frames if start <= item[0] < end][:FRAMES_PER_CLIP]
        score, detail = _score_frames([p for _t, p in inside])
        scored.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "seconds": round(end - start, 2),
            "score": round(score, 3),
            "detail": round(detail, 3),
            "has_text": False,
            "_frames": [p for _t, p in inside],
        })

    # 2) 只对分数最高的前 N 段做 OCR / 人形检测（这才是真正会被选中的）
    if ocr_ready() and len(scored) > 0:
        top = sorted(scored, key=lambda c: -c["score"])[: min(ocr_top, len(scored))]
        for clip in top:
            if time.time() - started > SELECT_BUDGET:
                logger.warning("选镜超时，剩余段跳过 OCR 检测")
                break
            paths = clip["_frames"][:2]     # 每段最多查 2 帧，够判断了
            if not paths:
                continue
            text_ratio = _text_frame_ratio(paths)
            person_ratio = _person_ratio(paths)
            clip["has_text"] = (
                text_ratio > TEXT_FRAME_RATIO or person_ratio > PERSON_FRAME_RATIO
            )

    for clip in scored:
        clip.pop("_frames", None)

    if prefer_clean:
        scored.sort(key=lambda c: (c["has_text"], -c["score"]))
    _save_cache(key, scored)
    logger.info("智能选镜完成：%d 段，耗时 %.1fs，最高分 %.2f",
                len(scored), time.time() - started, scored[0]["score"] if scored else 0)
    return scored


def _hook_order(chosen: list[dict], hook_first: bool = True) -> list[dict]:
    """把最抓人的一段提到最前，其余按拍摄顺序跟着走（绕一圈，不跳乱）。

    开场只从前面 85% 里挑：万一最高分在片尾，从最后一秒开场再绕回门口，
    人看着会晕。

    排序键是 detail（边缘密度）而不是 score。画质分在这里没用——亮度/饱和度
    都是"越接近理想越高"，室内实拍几乎全部打平，等于没区分。真正决定
    「第一眼抓不抓人」的是信息量：有家具、有隔断、有窗框的镜头边缘密，
    白墙和空走廊几乎是零。
    """
    if not hook_first or len(chosen) <= 1:
        return chosen
    head = max(1, int(round(len(chosen) * 0.85)))
    best = max(
        range(head),
        key=lambda i: (
            not chosen[i]["has_text"],
            chosen[i].get("detail", 0.0),
            chosen[i]["score"],
        ),
    )
    return chosen[best:] + chosen[:best]


def pick_clips(path: str | Path, needed: int, *, hook_first: bool = True) -> list[tuple[float, float]]:
    """选 needed 个镜头，返回 [(start, end)]。

    规则：blocked 的段垫底；同一段不连用两次；不够就重复用最好的几段，
    重复时间隔尽量远。

    hook_first：第一个镜头用画质最高的那段。抖音的流量几乎由前 3 秒决定，
    开头是一面白墙还是整面落地窗，完播率差一个档。剩下的按拍摄顺序从开场
    镜头往后绕一圈——保持「进门往里走」的空间感，只把最抓人的那一下提到最前。
    """
    clips = candidate_clips(path, ocr_top=max(needed + 4, OCR_TOP))
    good = [c for c in clips if not c["has_text"] and c["seconds"] >= MIN_CLIP]
    backup = [c for c in clips if c["has_text"] and c["seconds"] >= MIN_CLIP]
    pool = good + backup
    if not pool:
        duration = probe_duration(Path(_windows_path(path)))
        return [(0.0, min(duration, 4.0))]

    chosen: list[dict] = []
    used: dict[int, int] = {}
    for _index in range(needed):
        # 每轮挑「用得最少、没穿帮、分数最高」的；blocked 段计数上预支一千次，
        # 只有干净镜头全用光了才轮得到它们
        best_index, best_key = 0, None
        for position, clip in enumerate(pool):
            times_used = used.get(position, 0) + (1000 if clip["has_text"] else 0)
            key = (times_used, -clip["score"])
            if best_key is None or key < best_key:
                best_index, best_key = position, key
        chosen.append(pool[best_index])
        used[best_index] = used.get(best_index, 0) + 1

    chosen.sort(key=lambda c: c["start"])
    chosen = _hook_order(chosen, hook_first)
    result = [(c["start"], min(c["end"], c["start"] + MAX_CLIP)) for c in chosen]

    # 同一段被用了两次时会相邻出现，插花分散开
    for i in range(len(result) - 1):
        if result[i] != result[i + 1]:
            continue
        for j in range(i + 2, len(result)):
            if result[j] != result[i] and result[j - 1] != result[i] and (
                j + 1 >= len(result) or result[j + 1] != result[i]
            ):
                result[i + 1], result[j] = result[j], result[i + 1]
                break
    return result
