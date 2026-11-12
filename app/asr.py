"""本地语音与字幕读取：把一段参考视频「听懂」并「看清」。

风格模板要模仿三样东西：脚本结构、口播文案、字幕文案。它们藏在两个地方：

- 声音里 —— 用 faster-whisper 在本地转写（有显卡走 fp16，没有就退 CPU）；
- 画面上 —— 剪映导出的片子通常把字幕烧进画面，声音里只剩 BGM。
  这种情况靠 RapidOCR 抽帧识别底部字幕，按时间合并成句。

两条路都返回同样的 (start, end, text) 结构，上层不必关心文案是从哪来的。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 模型放在项目里，换机器连目录一起拷走就能用
ASR_MODEL_DIR = Path(os.getenv("ASR_MODEL_DIR") or BASE_DIR / "models" / "faster-whisper-large-v3-turbo")

_MODEL_LOCK = threading.Lock()
_MODELS: dict[str, object] = {}
# ctranslate2 要 CUDA 12 的 cuBLAS，本机 torch 是 CUDA 13，第一次会失败；
# 失败一次就记住，后续直接走 CPU，不要每条视频都撞一次墙。
_CUDA_BROKEN: dict[str, bool] = {"value": False}


logger = logging.getLogger(__name__)


class AsrError(RuntimeError):
    pass


# ------------------------------------------------------------------ 口播

def model_ready() -> bool:
    """权重齐了才算能用，避免第一次调用时才报一个看不懂的错。"""
    return (ASR_MODEL_DIR / "model.bin").is_file() and (ASR_MODEL_DIR / "tokenizer.json").is_file()


def _pick_device() -> tuple[str, str]:
    """优先显卡；显存不够或驱动不对时安静地退回 CPU，不要让整条流水线挂掉。"""
    forced = (os.getenv("ASR_DEVICE") or "").strip().lower()
    if forced in {"cpu", "cuda"}:
        return forced, ("float16" if forced == "cuda" else "int8")
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _load_model():
    key = f"{ASR_MODEL_DIR}|{_pick_device()[0]}"
    with _MODEL_LOCK:
        model = _MODELS.get(key)
        if model is None:
            if not model_ready():
                raise AsrError(
                    f"本地语音模型未就绪：{ASR_MODEL_DIR} 缺少 model.bin。"
                    "可用 tools/fetch_asr_model.py 从魔搭下载。"
                )
            from faster_whisper import WhisperModel

            device, compute = _pick_device()
            if _CUDA_BROKEN.get("value") and device == "cuda":
                device, compute = "cpu", "int8"
            model = WhisperModel(str(ASR_MODEL_DIR), device=device, compute_type=compute)
            _MODELS[key] = model
    return model


def _windows_path(path: str | Path) -> str:
    """把 MSYS 风格路径（/d/xxx）换成 Windows 路径（D:/xxx）。

    命令行里传进来的路径常是 Git Bash 形式，Python 自己认识，
    但 ffmpeg.exe 不认识，会直接报「找不到文件」。
    """
    text = str(path)
    match = re.match(r"^/([a-zA-Z])/(.*)$", text)
    if os.name == "nt" and match:
        return f"{match.group(1).upper()}:/{match.group(2)}"
    return text


def _decode_audio(path: str | Path) -> "object":
    """用项目自带的 ffmpeg 解成 16k 单声道 float32。

    faster-whisper 默认走 PyAV 解码，而 PyAV 的版本和行为经常对不上
    （本机就遇到过 metadata_errors 参数不被接受）。ffmpeg 反正是硬依赖，
    干脆自己解码，结果稳定、报错也好懂。
    """
    import subprocess

    import numpy as np

    from .shots import _find_binary

    ffmpeg = _find_binary("ffmpeg")
    completed = subprocess.run(
        [ffmpeg, "-v", "error", "-i", _windows_path(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "ignore").strip()[:300]
        raise AsrError(f"音频解码失败：{message or '未知错误'}")
    samples = np.frombuffer(completed.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        raise AsrError("音频解码结果为空，视频可能没有音轨")
    return samples


def transcribe(path: str | Path, language: str = "zh") -> list[dict]:
    """返回 [{start, end, text}]，时间单位秒。"""
    audio = _decode_audio(path)
    try:
        segments, _info = _run(_load_model(), audio, language)
    except Exception as error:  # noqa: BLE001 - 显卡不可用是环境问题，不是数据问题
        message = str(error).lower()
        if "cublas" in message or "cudnn" in message or "cuda" in message:
            _CUDA_BROKEN["value"] = True
            with _MODEL_LOCK:
                _MODELS.clear()
            logger.warning("显卡转写不可用（%s），改用 CPU", str(error)[:120])
            segments, _info = _run(_load_model(), audio, language)
        else:
            raise
    rows = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            rows.append({"start": round(float(seg.start), 2), "end": round(float(seg.end), 2), "text": text})
    return rows


# ------------------------------------------------------------------ 字幕

_OCR_LOCK = threading.Lock()
_OCR = None


def _load_ocr():
    global _OCR
    with _OCR_LOCK:
        if _OCR is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except Exception as error:  # pragma: no cover - 取决于是否装了 OCR
                raise AsrError(f"字幕识别不可用（未安装 rapidocr_onnxruntime）：{error}") from error
            _OCR = RapidOCR()
    return _OCR


def ocr_ready() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except Exception:
        return False


_CJK = re.compile(r"[\u4e00-\u9fff]")


_TEXT_BANDS = ((0.08, 0.36), (0.56, 0.90))  # 角标区、字幕区（占画面高度的比例）
_THREAD_LOCAL = threading.local()


def _ocr_for_thread():
    """RapidOCR 的会话不宜跨线程共用，每个线程各持一份。"""
    ocr = getattr(_THREAD_LOCAL, "ocr", None)
    if ocr is None:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
        _THREAD_LOCAL.ocr = ocr
    return ocr


def _ocr_frame(item: tuple[float, str]) -> list[dict]:
    timestamp, image_path = item
    import cv2

    image = cv2.imread(str(image_path))
    if image is None:
        return []
    height = image.shape[0]
    ocr = _ocr_for_thread()
    rows: list[dict] = []
    for top, bottom in _TEXT_BANDS:
        crop = image[int(height * top) : int(height * bottom)]
        if crop.size == 0:
            continue
        # 字幕字号在 1080 宽的画面里很大，缩到 720 宽识别率不掉，速度快一倍
        scale = 1.0
        if crop.shape[1] > 720:
            scale = 720 / crop.shape[1]
            crop = cv2.resize(crop, (720, int(crop.shape[0] * scale)))
        result, _ = ocr(crop)
        for entry in result or []:
            box, text, score = entry[0], str(entry[1]).strip(), float(entry[2])
            if score < 0.5 or not _CJK.search(text):
                continue
            y = (int(height * top) + float(box[0][1]) / scale) / height
            rows.append({"start": round(timestamp, 2), "end": round(timestamp, 2),
                         "text": text, "y": round(y, 3)})
    return rows


def read_captions(frames: list[tuple[float, str]], workers: int = 4) -> list[dict]:
    """识别每帧上方角标与下方字幕，带上纵向位置，再按时间合并。

    frames 是 [(时间秒, 图片路径)]，通常由 ffmpeg 抽帧得到。
    返回 [{start, end, text, y}]。只 OCR 两条文字带而不是整帧，
    速度快一倍多，也不会被墙上的招牌、门牌干扰。
    """
    if not ocr_ready():
        raise AsrError("字幕识别不可用（未安装 rapidocr_onnxruntime）")

    from concurrent.futures import ThreadPoolExecutor

    rows: list[dict] = []
    if workers > 1 and len(frames) > 8:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(_ocr_frame, frames):
                rows.extend(part)
    else:
        for item in frames:
            rows.extend(_ocr_frame(item))
    return _merge_same_text(rows)


def _merge_same_text(rows: list[dict]) -> list[dict]:
    """同一行字连续出现时合成一段，避免每秒一条重复记录。"""
    merged: list[dict] = []
    for row in sorted(rows, key=lambda r: r["start"]):
        same = None
        for item in reversed(merged):
            if item["text"] == row["text"] and abs(item["y"] - row["y"]) < 0.08:
                same = item
                break
        if same is not None and row["start"] - same["end"] <= 1.6:
            same["end"] = row["end"]
            continue
        merged.append(dict(row))
    return merged


def _run(model, audio, language: str):
    return model.transcribe(
        audio,
        language=language,
        vad_filter=True,          # 有 BGM 的片子必须开，否则会把音乐当成说话
        vad_parameters={"min_silence_duration_ms": 400},
        beam_size=5,
        condition_on_previous_text=False,
    )


def probe_capabilities() -> dict:
    device, compute = _pick_device()
    return {
        "speech_model": str(ASR_MODEL_DIR),
        "speech_ready": model_ready(),
        "speech_device": device,
        "speech_compute": compute,
        "caption_ocr_ready": ocr_ready(),
    }
