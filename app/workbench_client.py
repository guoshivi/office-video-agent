import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .db import DATA_DIR, EXPORT_DIR, get_settings, transaction, utc_now
from .hygiene import clean_internal_sentences
from .services import get_task

logger = logging.getLogger(__name__)

RENDER_LOCK = threading.Lock()


class WorkbenchError(RuntimeError):
    pass


def _configuration() -> tuple[str, str, Path]:
    settings = get_settings()
    base_url = (settings.get("workbench_url") or os.getenv("WORKBENCH_URL", "")).strip().rstrip("/")
    if not base_url:
        raise WorkbenchError("尚未配置视频工作台 API 地址")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WorkbenchError("视频工作台 API 地址格式无效")
    origin = os.getenv("WORKBENCH_ORIGIN", "").strip() or f"{parsed.scheme}://{parsed.netloc}"
    raw_output = settings.get("workbench_output_root") or str(DATA_DIR / "workbench_outputs")
    output_root = Path(raw_output)
    if not output_root.is_absolute():
        output_root = (DATA_DIR.parent / output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    return base_url, origin, output_root


def _remember_material_root(root: Path) -> None:
    """把自动探测到的素材根目录记下来，WebUI 里能看到实际用了哪一块盘。"""
    with transaction() as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES ('workbench_material_root', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(root),),
        )


def _set_task_state(task_id: str, status: str, stage: str, progress: int, error: str = "") -> None:
    with transaction() as connection:
        connection.execute(
            """UPDATE tasks SET status=?, stage=?, progress=?, error_message=?, updated_at=?
               WHERE id=?""",
            (status, stage, max(0, min(100, int(progress))), error, utc_now(), task_id),
        )


class WorkbenchClient:
    def __init__(self, base_url: str, origin: str):
        self.base_url = base_url
        self.origin = origin
        self.http = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(60, connect=10),
            headers={"Accept": "application/json", "Origin": origin},
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self.http.close()

    def _request(self, method: str, path: str, payload=None, timeout: float = 60):
        try:
            response = self.http.request(method, path, json=payload, timeout=timeout)
        except httpx.HTTPError as error:
            raise WorkbenchError(f"无法连接视频工作台：{error}") from error
        if response.status_code >= 400:
            text = response.text[:500]
            try:
                data = response.json()
                text = data.get("error") or data.get("message") or text
            except ValueError:
                pass
            raise WorkbenchError(f"视频工作台返回 {response.status_code}：{text}")
        try:
            return response.json()
        except ValueError as error:
            raise WorkbenchError("视频工作台返回了无法识别的响应") from error

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, payload=None, timeout: float = 60):
        return self._request("POST", path, payload or {}, timeout)

    def create_product(self, name: str) -> dict:
        return self.post("/api/products", {"name": name})

    def set_copy(self, product_id: str, script: str) -> dict:
        return self.post(f"/api/products/{product_id}/copy", {"text": script, "variants": []})

    def set_source(self, product_id: str, path: Path) -> dict:
        return self.post(f"/api/products/{product_id}/source", {"path": str(path)})

    def set_output(self, product_id: str, path: Path) -> dict:
        return self.post(f"/api/products/{product_id}/output", {"path": str(path)})

    def request_index(self, product_id: str) -> dict:
        return self.post(f"/api/products/{product_id}/index", {}, timeout=600)

    def enqueue(self, product_id: str, script: str, settings: dict) -> dict:
        return self.post(
            f"/api/products/{product_id}/jobs",
            {"count": 1, "text": script, "settings": settings},
            timeout=900,
        )

    def download_result(self, product_id: str, job_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with self.http.stream(
                "GET",
                f"/api/results/{job_id}",
                params={"product_id": product_id},
                timeout=httpx.Timeout(1800, connect=10),
            ) as response:
                if response.status_code >= 400:
                    raise WorkbenchError(f"成片下载失败：HTTP {response.status_code}")
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        output.write(chunk)
            if temporary.stat().st_size == 0:
                raise WorkbenchError("工作台返回的成片为空")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def workbench_status() -> dict:
    try:
        base_url, origin, _ = _configuration()
        client = WorkbenchClient(base_url, origin)
        try:
            worker = client.get("/api/worker")
        finally:
            client.close()
        return {
            "configured": True,
            "online": True,
            "url": base_url,
            "worker": worker,
        }
    except Exception as error:
        return {
            "configured": bool(get_settings().get("workbench_url")),
            "online": False,
            "url": get_settings().get("workbench_url", ""),
            "error": str(error),
        }


def _material_root(client: "WorkbenchClient") -> Path:
    """工作台只接受位于授权 NAS 根目录内的素材/输出路径。

    优先用手工配置的 workbench_material_root；没配就向工作台要 /api/roots，
    挑第一个本机真实存在的根目录，把素材镜像进去，而不是去改对方配置。
    """
    configured = (get_settings().get("workbench_material_root") or "").strip()
    if configured:
        root = Path(configured)
        if root.is_dir():
            return root
        raise WorkbenchError(f"素材根目录不存在：{configured}")

    try:
        roots = client.get("/api/roots") or []
    except Exception as error:
        raise WorkbenchError(f"无法读取工作台授权目录：{error}") from error
    for item in roots:
        candidate = Path(str(item.get("path") or ""))
        if candidate.is_dir():
            return candidate
    raise WorkbenchError(
        "工作台的授权目录都不在本机可访问，请在系统设置里填写 workbench_material_root"
    )


def _prepare_source(task: dict, material_root: Path) -> Path:
    source_video = Path(task["source_video_path"])
    if not source_video.is_file():
        raise WorkbenchError("任务原始视频不存在")
    # 工作台会递归扫描素材目录后再做视觉索引，所以按任务隔离一个子目录
    source_dir = material_root / "ova-agent" / task["task_no"] / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / source_video.name
    if not target.exists():
        try:
            os.link(source_video, target)
        except OSError:
            shutil.copy2(source_video, target)
    return source_dir


def _flag(settings: dict, key: str, default: str = "0") -> bool:
    return str(settings.get(key, default)).strip() not in {"", "0", "false", "False", "off"}


def _render_settings(task: dict, safe_mode: bool = False) -> dict:
    width, height = (1080, 1920) if task["aspect_ratio"] == "9:16" else (1920, 1080)
    speaker = "Eric" if task["voice"] == "male_professional" else "Serena"
    settings = get_settings()
    try:
        gap = float(settings.get("render_sentence_gap") or 0.22)
    except ValueError:
        gap = 0.22
    return {
        "voice": True,
        "speaker": speaker,
        "voice_engine": "qwen",
        "subtitles": True,
        "subtitle_size": 54,
        "subtitle_bottom": 360,
        "subtitle_border": 3,
        "subtitle_style": "classic",
        "subtitle_highlight": True,
        "bgm": False,
        "dedup": "light",
        "speed": 1.0,
        "shot_reuse": 2,
        # 工作台原生支持这些，之前没显式传就吃它的默认值，
        # 现在按「中介发圈」这个场景定死：留贴纸、不要花字刷屏、不加音效
        "sticker_enabled": False if safe_mode else _flag(settings, "render_sticker", "0"),
        "flower_enabled": False if safe_mode else _flag(settings, "render_flower", "0"),
        "sfx_enabled": False if safe_mode else _flag(settings, "render_sfx", "0"),
        "sentence_gap_seconds": 0.10 <= gap <= 1.0 and gap or 0.22,
        "target_seconds": int(task["target_duration"]),
        "width": width,
        "height": height,
        "fps": 30,
        "codec": "h264",
    }


def _poll_index(client: WorkbenchClient, product_id: str, task_id: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        state = client.get(f"/api/products/{product_id}/index")
        if state.get("ready"):
            _set_task_state(task_id, "processing", "视觉索引已准备", 40)
            return
        tasks = state.get("tasks") or []
        latest = tasks[-1] if tasks else {}
        if latest.get("status") in {"failed", "cancelled", "interrupted"}:
            raise WorkbenchError(f"素材索引失败：{latest.get('error') or latest.get('status')}")
        indexed, total = latest.get("indexed") or 0, latest.get("total") or 0
        progress = 20 + (16 * indexed / total if total else 0)
        _set_task_state(task_id, "processing", f"正在建立视觉索引 {indexed}/{total}", progress)
        time.sleep(8)
    raise WorkbenchError("素材索引超时")


def _poll_job(
    client: WorkbenchClient, product_id: str, job_id: str, task_id: str, deadline: float
) -> dict:
    while time.monotonic() < deadline:
        jobs = client.get(f"/api/jobs?product_id={product_id}")
        job = next((item for item in jobs if item.get("id") == job_id), None)
        if job:
            status = job.get("status")
            progress = 45 + int((job.get("progress") or 0) * 0.5)
            stage = job.get("stage") or job.get("status") or "处理中"
            _set_task_state(task_id, "processing", f"工作台：{stage}", progress)
            if status == "done":
                return job
            if status in {"failed", "cancelled", "interrupted"}:
                error = job.get("error")
                if isinstance(error, dict):
                    detail = " / ".join(
                        str(error.get(key) or "") for key in ("stage", "check", "message")
                    ).strip(" /")
                else:
                    detail = str(error or status)
                raise WorkbenchError(f"工作台出片失败：{detail}")
        time.sleep(8)
    raise WorkbenchError("工作台出片超时")


# 这些失败重试也没用，只会白白再等一轮
_NO_RETRY_HINTS = (
    "连不上", "无法读取工作台", "工作台未配置", "请先在系统设置", "不在授权", "找不到 ffmpeg",
    # 显卡被别的程序占着，立刻重试必然还是撞车，只会让人多等一轮
    "显卡正忙", "显存",
)


def _worth_retrying(message: str) -> bool:
    return not any(hint in message for hint in _NO_RETRY_HINTS)


def _failure_stage(message: str) -> str:
    """把工作台的报错翻成中介看得懂、能自己处理的一句话。"""
    if "显卡正忙" in message or "显存" in message:
        return "显卡被其他程序占用，关掉 ComfyUI/游戏后再点出片"
    if "不在授权" in message:
        return "素材目录不在工作台授权范围内，检查 workbench_material_root"
    return "出片失败"


def _render_once(task_id: str, task: dict, safe_mode: bool = False) -> None:
    """完整跑一遍出片。safe_mode 关掉工作台那边由模型决定的视觉包装。"""
    _set_task_state(task_id, "processing", "正在准备素材", 5)
    # 内部提醒句必须在送进配音之前摘掉：等渲染完才在审核台发现，
    # 就要重跑一次十几分钟的渲染，而且这条片子当天大概率发不出去。
    script, removed = clean_internal_sentences(task["script"])
    if removed:
        logger.warning(
            "口播里有 %d 句内部用语，已在出片前摘掉（任务 %s）：%s",
            len(removed), task["task_no"], "；".join(removed)[:200],
        )
    base_url, origin, _ = _configuration()
    client = WorkbenchClient(base_url, origin)
    try:
        material_root = _material_root(client)
        source_dir = _prepare_source(task, material_root)
        # 输出目录同样要在授权根目录内，否则工作台会 400
        output_dir = material_root / "ova-agent" / task["task_no"] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        _remember_material_root(material_root)
        _set_task_state(task_id, "processing", "正在创建视频项目", 10)
        product = client.create_product(f"{task['task_no']} {task['source']}")
        product_id = product["id"]
        client.set_copy(product_id, script)
        client.set_source(product_id, source_dir)
        client.set_output(product_id, output_dir)
        _set_task_state(task_id, "processing", "正在启动视觉索引", 20)
        initial = client.get(f"/api/products/{product_id}/index")
        if not initial.get("ready"):
            client.request_index(product_id)
        _poll_index(client, product_id, task_id, time.monotonic() + 45 * 60)
        _set_task_state(task_id, "processing", "正在生成配音与视频", 45)
        job = client.enqueue(product_id, script, _render_settings(task, safe_mode))
        job_id = job["id"] if isinstance(job, dict) else job[0]["id"]
        _poll_job(client, product_id, job_id, task_id, time.monotonic() + 60 * 60)
        destination = EXPORT_DIR / task["task_no"] / "final.mp4"
        client.download_result(product_id, job_id, destination)
        with transaction() as connection:
            connection.execute(
                """UPDATE tasks SET status='completed', stage='已完成', progress=100,
                   output_video_path=?, error_message='', updated_at=? WHERE id=?""",
                (str(destination), utc_now(), task_id),
            )
    finally:
        client.close()


def render_task(task_id: str) -> None:
    task = get_task(task_id)
    if task is None:
        return
    with RENDER_LOCK:
        try:
            _render_once(task_id, task)
            return
        except Exception as error:
            message = str(error)[:2000] if str(error) else error.__class__.__name__

        # 工作台的贴纸/花字/音效是模型临时决定的，模型给错一个资源名整条就废。
        # 这种失败不该让中介白等十分钟，关掉包装再跑一次。
        if not _worth_retrying(message):
            _set_task_state(task_id, "failed", _failure_stage(message), 0, message)
            return
        _set_task_state(task_id, "processing", "首次出片失败，正在用最稳妥的参数重试", 50)
        try:
            _render_once(task_id, task, safe_mode=True)
        except Exception as second:
            detail = str(second)[:1500] if str(second) else second.__class__.__name__
            _set_task_state(
                task_id, "failed", "出片失败（已降级重试）", 0,
                f"{message}\n\n降级重试仍失败：{detail}",
            )
