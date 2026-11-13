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
from .shots import probe_duration

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
    逐个做**可写探测**（建一个临时子目录再删掉）——Windows 对没登录的
    NAS 共享，is_dir() 都可能返回 True，但一建目录就报「用户名或密码
    不正确」。只有探测通过的根才敢用。
    """
    configured = (get_settings().get("workbench_material_root") or "").strip()
    if configured:
        root = Path(configured)
        if _root_writable(root):
            return root
        raise WorkbenchError(f"素材根目录不可用或没有写权限：{configured}")

    try:
        roots = client.get("/api/roots") or []
    except Exception as error:
        raise WorkbenchError(f"无法读取工作台授权目录：{error}") from error
    problems: list[str] = []
    for item in roots:
        candidate = Path(str(item.get("path") or ""))
        if _root_writable(candidate):
            return candidate
        problems.append(str(candidate))
    raise WorkbenchError(
        "工作台的授权目录在本机都不可写（NAS 未登录或目录不存在）："
        + "；".join(problems)
        + "。请先在本机登录 NAS（net use），或在系统设置里把渲染引擎切到 local。"
    )


def _root_writable(root: Path) -> bool:
    """探测根目录真的能建文件：能建临时目录并删掉才算通过。"""
    try:
        if not root.is_dir():
            return False
        probe = root / ".ova-write-probe"
        probe.mkdir(parents=True, exist_ok=False)
        probe.rmdir()
        return True
    except OSError:
        return False


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


def _task_value(task, key: str) -> str:
    """读任务级覆盖值。老的 row 没有这列、或填的是空串，都返回空让设置兜底。"""
    try:
        if key not in task.keys():
            return ""
        return str(task[key] or "").strip()
    except (AttributeError, TypeError, IndexError, KeyError):
        return ""


def _render_settings(task: dict, safe_mode: bool = False) -> dict:
    from . import tts

    width, height = (1080, 1920) if task["aspect_ratio"] == "9:16" else (1920, 1080)
    # 男声预设都叫 male_*：按前缀判；豆包键（doubao_*）看兜底键是不是 male 开头
    voice_key = str(task["voice"] or "").strip().lower()
    entry = tts.DOUBAO_VOICES.get(voice_key)
    edge_key = entry[2] if entry else voice_key
    speaker = "Eric" if edge_key.startswith("male") else "Serena"
    settings = get_settings()
    try:
        gap = float(settings.get("render_sentence_gap") or 0.22)
    except ValueError:
        gap = 0.22
    # 语速：设置里存的是 "+10%" 这种百分比，工作台那边要倍率。
    # 任务上选过就听任务的——同一条片子改语速不该逼人先去改全局设置。
    rate = _task_value(task, "tts_rate") or str(settings.get("render_tts_rate") or "").strip()
    try:
        speed = round(1 + float(rate.rstrip("%")) / 100, 2) if rate else 1.0
    except ValueError:
        speed = 1.0
    # 花字开关：任务上写了 0/1 就听任务的，否则听设置
    task_flower = _task_value(task, "flower")
    if task_flower == "1":
        flower_on = True
    elif task_flower == "0":
        flower_on = False
    else:
        flower_on = _flag(settings, "render_flower", "1")
    # target_duration=0 表示「根据原素材时长出片」：探测原始视频的真实长度
    target_seconds = int(task["target_duration"])
    if target_seconds <= 0:
        try:
            target_seconds = int(probe_duration(task["source_video_path"]))
        except Exception as error:  # noqa: BLE001 - 探测失败就退回默认值
            logger.warning("按原素材时长出片：读不到素材时长（%s），退回 45 秒", error)
            target_seconds = 45
    return {
        "voice": True,
        "speaker": speaker,
        "voice_engine": "qwen",
        "vox_voice": "natural_female",
        "subtitles": True,
        "subtitle_size": {"small": 42, "medium": 54, "large": 66}.get(
            _task_value(task, "subtitle_size")
            or str(settings.get("render_subtitle_size") or "medium"),
            54,
        ),
        # 工作台自己的三档位置：靠上 560 / 居中 380 / 靠下 240
        "subtitle_bottom": {"high": 560, "middle": 380, "low": 240}.get(
            _task_value(task, "subtitle_bottom")
            or str(settings.get("render_subtitle_bottom") or "middle"),
            380,
        ),
        "subtitle_border": 3,
        "subtitle_style": "classic",
        # 让模型自己挑句子里该高亮的词，比固定规则更接近人工出的效果
        "highlight_source": str(settings.get("render_highlight_source") or "ai").strip() or "ai",
        "subtitle_highlight": _flag(settings, "render_subtitle_highlight", "1"),
        "bgm": False,
        "bgm_path": "",
        "bgm_volume": 0.2,
        "voice_volume": 1.0,
        # 工作台默认 off；开 light 会为了躲平台判重去动画面，画质反而掉
        "dedup": "off",
        "speed": speed,
        # 1 = 一个镜头只用一次；2 会让同一段画面重复出现，成片显廉价
        "shot_reuse": 1,
        # 下面这几个是三件套的开关。默认跟工作台界面一致（全开），
        # 失败时 render_task 会用 safe_mode 关掉重跑一次，不会让中介干等
        "sticker_enabled": False if safe_mode else _flag(settings, "render_sticker", "1"),
        "flower_enabled": False if safe_mode else flower_on,
        "sfx_enabled": False if safe_mode else _flag(settings, "render_sfx", "1"),
        "flower_style": str(settings.get("render_flower_style") or "auto").strip() or "auto",
        "flower_density": str(settings.get("render_flower_density") or "high").strip() or "high",
        # 美颜和滤镜对房源实拍是减分项：墙面色偏了反而显得房子不对
        "beauty": False,
        "beauty_strength": 0.25,
        "visual_filter": "original",
        "visual_filter_strength": 0.35,
        "pronunciations": [],
        "subtitle_overrides": [],
        "cover": None,
        "intro": None,
        "outro": None,
        "logo_enabled": False,
        "overlay_enabled": False,
        "watermark_enabled": False,
        "sentence_gap_seconds": 0.10 <= gap <= 1.0 and gap or 0.22,
        "target_seconds": target_seconds,
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
    # 授权根目录是 NAS 共享而本机没有登录凭据，重试一次也是同样的错
    "用户名或密码不正确", "无法访问", "登录失败",
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
        engine = str(get_settings().get("render_engine") or "local").strip().lower()
        if engine == "local":
            _render_locally(task_id, task, "按设置使用本地渲染引擎")
            return
        try:
            _render_once(task_id, task)
            return
        except Exception as error:
            message = str(error)[:2000] if str(error) else error.__class__.__name__

        # 工作台的贴纸/花字/音效是模型临时决定的，模型给错一个资源名整条就废。
        # 这种失败不该让中介白等十分钟，关掉包装再跑一次。
        if not _worth_retrying(message):
            # 授权根目录不可达（NAS 没登录、目录不在本机）这类错误，
            # 降级重试也是白等，直接换本机渲染引擎兜底。
            _render_locally(task_id, task, f"工作台素材根不可用（{message[:120]}），改用本地渲染")
            return
        _set_task_state(task_id, "processing", "首次出片失败，正在用最稳妥的参数重试", 50)
        try:
            _render_once(task_id, task, safe_mode=True)
        except Exception as second:
            detail = str(second)[:1500] if str(second) else second.__class__.__name__
            # 降级重试也失败：最后再试一次本地渲染，能救回来就不用人工折腾
            try:
                _render_locally(task_id, task, "工作台两次出片均失败，最后改用本地渲染")
                return
            except Exception as local_error:
                local_detail = str(local_error)[:500] if str(local_error) else "未知错误"
            _set_task_state(
                task_id, "failed", "出片失败（已降级重试）", 0,
                f"{message}\n\n降级重试仍失败：{detail}\n\n本地渲染兜底也失败：{local_detail}",
            )


def _render_locally(task_id: str, task: dict, reason: str) -> None:
    """本机 ffmpeg 出片兜底。

    走独立子进程（app.render_cli）：渲染链路会加载 onnxruntime + torch，
    Windows 上这类 native 依赖偶尔会把宿主进程带崩。子进程崩了服务还活着，
    这条任务照样能拿到明确的失败原因。
    """
    import subprocess
    import sys

    from .db import BASE_DIR  # noqa: F401 - 只是为了确认工程根可用

    logger.warning("任务 %s 切换本地渲染：%s", task["task_no"], reason)
    _set_task_state(task_id, "processing", f"{reason}", 10)
    command = [sys.executable, "-m", "app.render_cli", task_id]
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _set_task_state(task_id, "failed", "本地渲染超时", 0, "本地渲染超过 30 分钟未完成")
        return

    payload: dict = {}
    for line in reversed((completed.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue

    if completed.returncode == 0 and payload.get("ok"):
        with transaction() as connection:
            connection.execute(
                """UPDATE tasks SET status='completed', stage='本地渲染完成', progress=100,
                   output_video_path=?, error_message='', updated_at=? WHERE id=?""",
                (str(payload.get("output") or ""), utc_now(), task_id),
            )
        return

    detail = payload.get("error") or (completed.stderr or "").strip()[-800:] or "子进程异常退出"
    _set_task_state(
        task_id, "failed", "本地渲染失败", 0,
        f"本地渲染子进程退出码 {completed.returncode}：{detail}",
    )
