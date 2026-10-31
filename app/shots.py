"""分镜：把一段实拍切成「镜头 → 一句话」。

中介现场拍摄是有顺序的（门头 → 前台 → 工位 → 会议室 → 窗外），
但工作台拿到的只是**一整段文案**，它按句切分后再逐句去视觉索引里找镜头。
所以「哪个场景配哪句文案」真正的把手在**文案怎么分句**上。

这里做两件事：
1. 按时间把原片切成若干段，每段抽一张代表帧，交给能看图的一方（WorkBuddy 多模态）标注；
2. 把标注结果按镜头顺序合成最终文案，一句一镜，写回任务。

故意不做自动场景检测：房源实拍镜头本身差异小，固定时长切段更可预测，
出了问题也容易解释，不会因为阈值调不好时好时坏。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .db import DATA_DIR, get_settings, json_dumps, transaction, utc_now
from .services import get_task

SHOT_DIR = DATA_DIR / "shots"
MAX_SHOT_TEXT = 24  # 一屏字幕的舒适上限，超了就提示拆句


class ShotError(RuntimeError):
    pass


# ------------------------------------------------------------------ ffmpeg

def _find_binary(name: str) -> str:
    """找 ffmpeg/ffprobe：配置 > 环境变量 > PATH > 工作台自带的那个。"""
    stem = "ffprobe" if "probe" in name else "ffmpeg"
    executable = f"{stem}.exe" if os.name == "nt" else stem

    configured = (get_settings().get("ffmpeg_path") or "").strip()
    if configured:
        path = Path(configured)
        # 允许填目录，也允许直接填 ffmpeg.exe
        candidate = path / executable if path.is_dir() else path
        if candidate.is_file():
            return str(candidate)
        raise ShotError(f"配置的 ffmpeg 路径不存在：{configured}")

    found = shutil.which(stem)
    if found:
        return found

    # 本机常见位置：工作台试用包、项目自带 bin
    for root in (Path("D:/video-pipeline-trial"), DATA_DIR / "bin"):
        if not root.is_dir():
            continue
        for candidate in root.glob(f"*/tools/{executable}"):
            if candidate.is_file():
                return str(candidate)
        for candidate in root.glob(executable):
            if candidate.is_file():
                return str(candidate)
    raise ShotError("找不到 ffmpeg/ffprobe，请在系统设置里填 ffmpeg_path")


def _run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as error:
        raise ShotError(f"ffmpeg 执行超时：{' '.join(command[:3])}") from error


def probe_duration(video: Path) -> float:
    result = _run([
        _find_binary("ffprobe"), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video),
    ])
    try:
        return float(result.stdout.strip())
    except ValueError as error:
        raise ShotError(f"读不出视频时长：{(result.stderr or '').strip()[:200]}") from error


# ------------------------------------------------------------------ 切段抽帧

def _segment_plan(duration: float, segment_seconds: float, max_shots: int, min_shots: int) -> list[tuple[float, float]]:
    if duration <= 0:
        raise ShotError("视频时长为 0，无法分段")
    count = int(round(duration / max(2.0, segment_seconds)))
    count = max(min_shots, min(max_shots, count))
    step = duration / count
    return [(round(i * step, 2), round((i + 1) * step, 2)) for i in range(count)]


def analyze(video_path: str, task_no: str, *, segment_seconds: float = 0, max_shots: int = 8, min_shots: int = 3, force: bool = False) -> dict:
    video = Path(video_path)
    if not video.is_file():
        raise ShotError("原始视频不存在")

    out_dir = SHOT_DIR / task_no
    manifest = out_dir / "shots.json"
    if manifest.is_file() and not force:
        cached = json.loads(manifest.read_text(encoding="utf-8"))
        if all(Path(shot["frame"]).is_file() for shot in cached.get("shots", [])):
            return cached

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("shot_*.jpg"):
        stale.unlink()

    seconds = segment_seconds or float(get_settings().get("shot_segment_seconds") or 6)
    duration = probe_duration(video)
    plan = _segment_plan(duration, seconds, max_shots, min_shots)

    ffmpeg = _find_binary("ffmpeg")
    shots = []
    for index, (start, end) in enumerate(plan, start=1):
        moment = round((start + end) / 2, 2)
        frame = out_dir / f"shot_{index:02d}.jpg"
        result = _run([
            ffmpeg, "-y", "-v", "error", "-ss", f"{moment}",
            "-i", str(video), "-frames:v", "1", "-q:v", "3", str(frame),
        ])
        if not frame.is_file():
            raise ShotError(f"抽帧失败（第 {index} 段）：{(result.stderr or '').strip()[:200]}")
        shots.append({
            "index": index,
            "start": start,
            "end": end,
            "at": moment,
            "frame": str(frame),
        })

    payload = {
        "task_no": task_no,
        "duration": round(duration, 2),
        "segment_seconds": seconds,
        "shots": shots,
        "storyboard": [],
    }
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_manifest(task_no: str) -> dict:
    manifest = SHOT_DIR / task_no / "shots.json"
    if not manifest.is_file():
        raise ShotError("还没有做过分镜分析，请先跑 analyze")
    return json.loads(manifest.read_text(encoding="utf-8"))


def frame_path(task_no: str, index: int) -> Path:
    path = SHOT_DIR / task_no / f"shot_{int(index):02d}.jpg"
    if not path.is_file():
        raise ShotError(f"第 {index} 段没有抽帧，请重新 analyze")
    return path


# ------------------------------------------------------------------ 分镜回填

def compose_script(storyboard: list[dict]) -> str:
    """一句一镜，句号收尾，保证工作台按预期切成 N 句。"""
    sentences = []
    for shot in sorted(storyboard, key=lambda item: int(item.get("index") or 0)):
        text = str(shot.get("text") or "").strip()
        if not text:
            continue
        if text[-1] not in "。！？":
            text += "。"
        sentences.append(text)
    return "".join(sentences)


def save_storyboard(task_id: str, storyboard: list[dict]) -> dict:
    task = get_task(task_id)
    if task is None:
        raise ShotError("任务不存在")

    clean = []
    for shot in storyboard:
        index = int(shot.get("index") or 0)
        text = str(shot.get("text") or "").strip()
        if index <= 0:
            raise ShotError("分镜序号必须从 1 开始")
        if not text:
            continue
        if len(text) > MAX_SHOT_TEXT:
            raise ShotError(
                f"第 {index} 句有 {len(text)} 字，超过一屏字幕的 {MAX_SHOT_TEXT} 字，请拆成两句"
            )
        clean.append({
            "index": index,
            "label": str(shot.get("label") or "").strip()[:40],
            "text": text,
        })

    if not clean:
        raise ShotError("分镜文案不能为空")

    script = compose_script(clean)
    manifest = load_manifest(task["task_no"])
    manifest["storyboard"] = clean
    (SHOT_DIR / task["task_no"] / "shots.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with transaction() as connection:
        connection.execute(
            """UPDATE tasks SET script=?, storyboard_json=?, status='script_ready',
               workbench_package_path='', updated_at=? WHERE id=?""",
            (script, json_dumps(clean), utc_now(), task_id),
        )
    return {
        "task_no": task["task_no"],
        "script": script,
        "storyboard": clean,
        "sentence_count": len(clean),
        "estimated_seconds": round(len(script) / 4.2, 1),
    }
