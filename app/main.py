import logging
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .channel import (
    ChannelError,
    bind_clip,
    drop_clip,
    list_clips,
    resolve as resolve_channel_property,
    save_clip,
)
from .db import DATA_DIR, UPLOAD_DIR, get_settings, init_db, transaction, utc_now
from .excel_import import import_excel
from .feishu.bot import get_bot, start_bot
from .semantic import is_configured, reindex_properties, search_properties, semantic_status
from .shots import (
    ShotError,
    analyze,
    frame_path,
    save_storyboard,
)
from .services import (
    create_task_record,
    export_workbench_package,
    generate_script,
    get_task,
    new_task_no,
    public_property,
    serialize_task,
    set_task_script,
)
from .workbench_client import render_task, workbench_status

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_EXCEL_SIZE = 20 * 1024 * 1024
MAX_VIDEO_SIZE = 2 * 1024 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
TASK_STATUSES = {"received", "script_ready", "exported", "processing", "review", "completed", "failed"}

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with transaction() as connection:
        connection.execute(
            """UPDATE tasks SET status='failed', stage='任务已中断',
               error_message='应用重启导致后台出片任务中断，请确认工作台状态后重新提交。',
               updated_at=? WHERE status='processing'""",
            (utc_now(),),
        )
    # 飞书长连接在独立线程里跑；没配凭据时只记录状态，不影响 WebUI
    try:
        start_bot()
    except Exception:
        logger.exception("飞书机器人启动失败")

    # 多维表格裁决轮询：受 review_bitable_sync 控制，没开就什么都不做
    try:
        from .review import bitable_bridge

        if not bitable_bridge.start_watcher():
            logger.info("多维表格审核轮询未启用（review_bitable_sync 未打开）")
    except Exception:
        logger.exception("多维表格审核轮询启动失败")

    # 已启用的发布服务在后台线程里拉起，别让"等浏览器启动"拖慢首页
    def _autostart_services() -> None:
        try:
            from . import ready

            started = ready.autostart()
            if started:
                logger.info("自动启动发布服务：%s", started)
        except Exception:
            logger.exception("自动启动发布服务失败")

    threading.Thread(target=_autostart_services, name="autostart", daemon=True).start()

    yield

    try:
        from .review import bitable_bridge

        bitable_bridge.stop_watcher()
    except Exception:
        pass
    try:
        get_bot().stop()
    except Exception:
        pass


app = FastAPI(title="房源视频工作台", version=__version__, lifespan=lifespan)


# 本地工具，静态资源永远用最新的那份。
# 不加这个头时浏览器会启发式缓存（按 Last-Modified 猜一个有效期），
# 改了 CSS 刷新还是旧样子——排查时很容易误判成"没改对"。
@app.middleware("http")
async def no_stale_statics(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/api/") and (
        path == "/" or path.endswith((".html", ".css", ".js", ".svg"))
    ):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# 发布与审核按领域拆到 app/api/，入口文件只负责装配。
# 放在 mount 之前注册，否则会被 StaticFiles 的 "/" 兜住。
from .api import publish as publish_api  # noqa: E402
from .api import ready as ready_api  # noqa: E402
from .api import review as review_api  # noqa: E402

app.include_router(publish_api.router)
app.include_router(ready_api.router)
app.include_router(review_api.router)


class ScriptUpdate(BaseModel):
    script: str = Field(min_length=1, max_length=5000)


class StatusUpdate(BaseModel):
    status: str
    error_message: str = Field(default="", max_length=2000)


class AudioModeUpdate(BaseModel):
    # 空字符串 = 跟随设置里的默认值
    audio_mode: str = Field(default="", max_length=20)


class SettingsUpdate(BaseModel):
    public_contact: str = Field(default="", max_length=300)
    # 0 = 新任务默认跟随原素材时长；否则 10–180 秒
    default_duration: int = Field(default=45, ge=0, le=180)
    default_aspect_ratio: str = Field(default="9:16")
    default_voice: str = Field(default="female_warm")
    workbench_path: str = Field(default="", max_length=1000)
    workbench_url: str = Field(default="", max_length=500)
    workbench_output_root: str = Field(default="", max_length=1000)
    workbench_material_root: str = Field(default="", max_length=1000)
    render_engine: str = Field(default="local", max_length=20)
    export_output_root: str = Field(default="", max_length=1000)
    local_audio_mode: str = Field(default="mix", max_length=20)
    # 本机出片的观感：花字贴片、BGM（曲库在 data/render-env/music）
    render_flower: str = Field(default="1", max_length=4)
    render_bgm: str = Field(default="1", max_length=4)
    render_bgm_mood: str = Field(default="", max_length=12)
    render_bgm_volume: float = Field(default=0.14, ge=0, le=0.5)
    # 大模型：留空表示不用 AI，文案走规则版
    llm_provider: str = Field(default="", max_length=20)
    llm_api_key: str = Field(default="", max_length=300)
    llm_base_url: str = Field(default="", max_length=300)
    llm_model: str = Field(default="", max_length=120)


def _ffmpeg_ready() -> bool:
    from .shots import ShotError, _find_binary

    try:
        _find_binary("ffmpeg")
        _find_binary("ffprobe")
    except ShotError:
        return False
    return True


@app.get("/api/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "mode": "local",
        "database": "sqlite",
        # 不只看 PATH：ffmpeg 是随包放在 vendor/ffmpeg 的，
        # 只认 PATH 会让"明明能出片"的机器显示未配置
        "ffmpeg_available": _ffmpeg_ready(),
        "workbench_configured": bool(settings.get("workbench_url")),
        "workbench_url": settings.get("workbench_url", ""),
        "semantic_search_configured": is_configured(),
        "semantic_model": semantic_status()["model"],
        "feishu_state": get_bot().state,
    }


@app.get("/api/dashboard")
def dashboard():
    with transaction() as connection:
        property_count = connection.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        review_count = connection.execute(
            "SELECT COUNT(*) FROM properties WHERE status='needs_review'"
        ).fetchone()[0]
        task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        completed_count = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='completed'"
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT t.*, p.region_norm, p.building_norm, p.room_norm,
                      p.area_raw, p.rent_raw, p.status AS property_status
               FROM tasks t JOIN properties p ON p.id=t.property_id
               ORDER BY t.created_at DESC LIMIT 6"""
        ).fetchall()
    return {
        "property_count": property_count,
        "review_count": review_count,
        "task_count": task_count,
        "completed_count": completed_count,
        "recent_tasks": [serialize_task(row, include_script=False) for row in rows],
    }


@app.get("/api/properties")
def list_properties(
    search: str = "",
    status: str = "",
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    clauses, params = [], []
    if search.strip():
        clauses.append("(region_norm LIKE ? OR building_norm LIKE ? OR room_norm LIKE ? OR area_raw LIKE ?)")
        keyword = f"%{search.strip()}%"
        params.extend([keyword] * 4)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with transaction() as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM properties{where}", params).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM properties{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"items": [public_property(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@app.get("/api/properties/{property_id}")
def get_property(property_id: str):
    with transaction() as connection:
        row = connection.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "房源不存在")
    return public_property(row)


@app.post("/api/properties/{property_id}/confirm")
def confirm_property(property_id: str):
    with transaction() as connection:
        row = connection.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "房源不存在")
        duplicate_count = connection.execute(
            "SELECT COUNT(*) FROM properties WHERE match_key=? AND match_key<>''",
            (row["match_key"],),
        ).fetchone()[0] if row["match_key"] else 0
        connection.execute(
            "UPDATE properties SET status='active', updated_at=? WHERE id=?",
            (utc_now(), property_id),
        )
        updated = connection.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
    return {**public_property(updated), "duplicate_count": duplicate_count}


@app.get("/api/workbench/status")
def get_workbench_status():
    return workbench_status()


@app.get("/api/semantic/status")
def get_semantic_status():
    return semantic_status()


@app.post("/api/semantic/reindex")
async def rebuild_semantic_index():
    try:
        return await reindex_properties()
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/semantic/search")
async def semantic_search(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=30, ge=1, le=200),
):
    try:
        items = await search_properties(q.strip(), limit)
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    return {"items": items, "total": len(items), "query": q}


@app.post("/api/properties/import")
async def import_properties(file: UploadFile = File(...)):
    filename = Path(file.filename or "listings.xlsx").name
    if Path(filename).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise HTTPException(400, "目前支持 .xlsx 和 .xlsm，原 .xls 文件请先另存为 .xlsx")
    content = await file.read(MAX_EXCEL_SIZE + 1)
    if len(content) > MAX_EXCEL_SIZE:
        raise HTTPException(413, "Excel 文件不能超过 20 MB")
    try:
        return import_excel(content, filename)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(400, f"无法导入工作簿：{error}") from error


@app.get("/api/tasks")
def list_tasks(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    with transaction() as connection:
        total = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        rows = connection.execute(
            """SELECT t.*, p.region_norm, p.building_norm, p.room_norm,
                      p.area_raw, p.rent_raw, p.status AS property_status
               FROM tasks t JOIN properties p ON p.id=t.property_id
               ORDER BY t.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
    return {"items": [serialize_task(row, include_script=False) for row in rows], "total": total}


class CopyGenerate(BaseModel):
    property_id: str = Field(default="")
    target_duration: int = Field(default=45, ge=0, le=180)
    extra: str = Field(default="", max_length=300)


class CopyPolish(BaseModel):
    property_id: str = Field(default="")
    script: str = Field(default="", max_length=4000)


def _require_property(property_id: str):
    if not property_id:
        raise HTTPException(400, "请先选择房源")
    with transaction() as connection:
        row = connection.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "房源不存在")
    return row


def _copy_facts(row) -> dict:
    """交给模型的事实清单。租金一律不传，避免它自己报价。"""
    raw = {
        "区域": row["region_norm"], "楼栋": row["building_norm"], "房号": row["room_norm"],
        "面积": row["area_raw"], "格局": row["partition_raw"],
        "朝向": row["orientation_raw"], "家具装修": row["furnishing_layout_raw"],
    }
    return {key: str(value).strip() for key, value in raw.items() if (value or "").strip()}


@app.get("/api/copy/status")
def copy_status():
    from .llm import status as llm_status

    return llm_status()


@app.post("/api/copy/generate")
def copy_generate(payload: CopyGenerate):
    """让大模型写一版口播稿。没配模型或调用失败时退回规则版，不让用户卡住。"""
    from .llm import LLMError, write_script as llm_write

    row = _require_property(payload.property_id)
    facts = _copy_facts(row)
    duration = payload.target_duration or 45
    try:
        script = llm_write(facts, duration, payload.extra.strip())
        return {"script": script, "source": "ai", "message": "AI 已按房源字段写好，可直接改"}
    except LLMError as error:
        script, warnings = generate_script(row, duration)
        return {
            "script": script,
            "source": "rules",
            "message": f"AI 不可用（{error}），已用规则版兜底",
            "warnings": warnings,
        }


@app.post("/api/copy/polish")
def copy_polish(payload: CopyPolish):
    from .llm import LLMError, polish_script

    text = (payload.script or "").strip()
    if len(text) < 10:
        raise HTTPException(400, "先把要润色的文案贴进来")
    facts = _copy_facts(_require_property(payload.property_id)) if payload.property_id else {}
    try:
        return {"script": polish_script(text, facts), "source": "ai"}
    except LLMError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/tasks")
async def create_task(
    property_id: str = Form(...),
    video: UploadFile = File(...),
    target_duration: int = Form(default=45),
    aspect_ratio: str = Form(default="9:16"),
    voice: str = Form(default="female_warm"),
    style: str = Form(default="professional"),
    script: str = Form(default=""),
    audio_mode: str = Form(default=""),
):
    if not (target_duration == 0 or 10 <= target_duration <= 180):
        raise HTTPException(400, "目标时长需在 10–180 秒之间，或填 0 表示跟随原素材时长")
    if aspect_ratio not in {"9:16", "16:9"}:
        raise HTTPException(400, "当前视频工作台支持 9:16 或 16:9")
    if audio_mode and audio_mode not in {"mix", "original", "voice", "mute"}:
        raise HTTPException(400, "原声只能是 保留 / 不要 / 只留原声 / 静音 四种之一")
    with transaction() as connection:
        property_row = connection.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
    if property_row is None:
        raise HTTPException(404, "请选择有效房源")

    original_filename = Path(video.filename or "source.mp4").name
    extension = Path(original_filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        raise HTTPException(400, "视频格式支持 MP4、MOV、M4V、AVI、MKV")

    # 先落盘到最终目录再建任务：失败时整目录删掉，不留半截数据
    task_no = new_task_no()
    task_dir = UPLOAD_DIR / task_no
    task_dir.mkdir(parents=True, exist_ok=True)
    video_path = task_dir / f"source{extension}"
    size = 0
    try:
        with video_path.open("wb") as output:
            while chunk := await video.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO_SIZE:
                    raise HTTPException(413, "单个视频不能超过 2 GB")
                output.write(chunk)
        if size == 0:
            raise HTTPException(400, "上传的视频为空")
        task_id = create_task_record(
            property_row=property_row,
            video_path=video_path,
            original_filename=original_filename,
            target_duration=target_duration,
            aspect_ratio=aspect_ratio,
            voice=voice,
            style=style,
            source="webui",
            task_no=task_no,
            script_override=script,
            audio_mode=audio_mode,
        )
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise
    return serialize_task(get_task(task_id))

@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    row = get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    return serialize_task(row)


@app.get("/api/tasks/{task_id}/video")
def source_video(task_id: str):
    row = get_task(task_id)
    if row is None or not Path(row["source_video_path"]).is_file():
        raise HTTPException(404, "原始视频不存在")
    return FileResponse(row["source_video_path"], filename=row["original_filename"])


@app.get("/api/tasks/{task_id}/output")
def output_video(task_id: str):
    row = get_task(task_id)
    if row is None or not row["output_video_path"]:
        raise HTTPException(404, "成片尚未生成")
    output_path = Path(row["output_video_path"])
    if not output_path.is_file():
        raise HTTPException(404, "成片文件不存在")
    return FileResponse(output_path, filename=f"{row['task_no']}.mp4", media_type="video/mp4")


@app.put("/api/tasks/{task_id}/script")
def update_script(task_id: str, payload: ScriptUpdate):
    if get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    if not set_task_script(task_id, payload.script):
        raise HTTPException(400, "口播文案不能为空")
    return serialize_task(get_task(task_id))


@app.put("/api/tasks/{task_id}/audio")
def update_audio_mode(task_id: str, payload: AudioModeUpdate):
    """这条任务保留不保留原声。空值表示回到设置里的默认。"""
    mode = payload.audio_mode.strip().lower()
    if mode and mode not in {"mix", "original", "voice", "mute"}:
        raise HTTPException(400, "原声只能是 保留 / 不要 / 只留原声 / 静音 四种之一")
    if get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    with transaction() as connection:
        connection.execute(
            "UPDATE tasks SET audio_mode=?, updated_at=? WHERE id=?",
            (mode, utc_now(), task_id),
        )
    return serialize_task(get_task(task_id))


@app.post("/api/tasks/{task_id}/export")
def export_task(task_id: str):
    try:
        return export_workbench_package(task_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/api/tasks/{task_id}/render", status_code=202)
def start_render(task_id: str, background_tasks: BackgroundTasks):
    settings = get_settings()
    engine = str(settings.get("render_engine") or "local").strip().lower()
    # 本地出片不需要工作台，别在最后一步拿"填工作台地址"拦住人
    if engine != "local" and not settings.get("workbench_url"):
        raise HTTPException(400, "请先在系统设置中填写视频工作台 API 地址")
    with transaction() as connection:
        row = connection.execute("SELECT status, script FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "任务不存在")
        if row["status"] == "processing":
            raise HTTPException(409, "该任务正在出片，请勿重复提交")
        if not row["script"].strip():
            raise HTTPException(400, "口播草稿为空，无法出片")
        connection.execute(
            """UPDATE tasks SET status='processing', stage='已排队，准备出片',
               progress=1, error_message='', updated_at=? WHERE id=?""",
            (utc_now(), task_id),
        )
    background_tasks.add_task(render_and_enqueue, task_id)
    message = "已开始出片，在这台电脑上跑，一会儿就能看" if engine != "workbench" else "已提交视频工作台，正在排队处理"
    return {"message": message, "task": serialize_task(get_task(task_id))}


def render_and_enqueue(task_id: str) -> None:
    """出片结束后自动进审核队列。

    放在装配层而不是 workbench_client 里：出片适配器不该知道"审核"这件事，
    而且审核台不可用时也绝不能影响出片本身。
    """
    render_task(task_id)
    try:
        row = get_task(task_id)
        if row is None or not row["output_video_path"]:
            return
        from .review.service import run_precheck
        from .review import store

        store.create(task_id)
        run_precheck(task_id)
    except Exception as error:  # noqa: BLE001 - 审核台的问题不能反噬出片结果
        logger.warning("出片成功但未能自动入审核队列（%s）：%s", task_id, error)


@app.post("/api/tasks/{task_id}/status")
def update_task_status(task_id: str, payload: StatusUpdate):
    if payload.status not in TASK_STATUSES:
        raise HTTPException(400, "无效任务状态")
    if get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    with transaction() as connection:
        connection.execute(
            "UPDATE tasks SET status=?, error_message=?, updated_at=? WHERE id=?",
            (payload.status, payload.error_message.strip(), utc_now(), task_id),
        )
    return serialize_task(get_task(task_id))


@app.get("/api/settings")
def read_settings():
    return get_settings()


@app.put("/api/settings")
def update_settings(payload: SettingsUpdate):
    values = payload.model_dump()
    values["default_duration"] = str(values["default_duration"])
    values["render_bgm_volume"] = f"{float(values['render_bgm_volume']):.2f}"
    if values["default_aspect_ratio"] not in {"9:16", "16:9"}:
        raise HTTPException(400, "当前视频工作台支持 9:16 或 16:9")
    if values["workbench_url"] and not values["workbench_url"].startswith(("http://", "https://")):
        raise HTTPException(400, "视频工作台 API 地址必须以 http:// 或 https:// 开头")
    if values["render_engine"] not in {"workbench", "local"}:
        raise HTTPException(400, "渲染引擎只能是 workbench 或 local")
    if values["local_audio_mode"] not in {"mix", "original", "voice", "mute"}:
        raise HTTPException(400, "本地出片原声只能是 mix / original / voice / mute")
    for flag in ("render_flower", "render_bgm"):
        if values[flag] not in {"0", "1"}:
            raise HTTPException(400, "花字/背景音乐开关只能是 0 或 1")
    if values["llm_provider"] and values["llm_provider"] not in {"ark", "deepseek"}:
        raise HTTPException(400, "模型服务商只支持 ark（豆包）或 deepseek")
    if values["llm_base_url"] and not values["llm_base_url"].startswith(("http://", "https://")):
        raise HTTPException(400, "模型接口地址必须以 http:// 或 https:// 开头")
    with transaction() as connection:
        for key, value in values.items():
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    return get_settings()


@app.get("/api/system/data-info")
def data_info():
    return {
        "data_directory": str(DATA_DIR),
        "note": "数据库、上传视频、成片和工作台输出均保存在本地；迁移时需要备份整个 data 目录。",
    }


# ------------------------------------------------------------------ 多入口收片
# WorkBuddy 桌面、IM 机器人、任何脚本都通过这三个接口喂料，业务逻辑只有一份。

class ClipBind(BaseModel):
    query: str = Field(default="", max_length=200)
    property_id: str = Field(default="", max_length=64)
    # 0 = 跟随原素材时长；否则 10–180 秒
    target_duration: int = Field(default=45, ge=0, le=180)
    aspect_ratio: str = "9:16"
    voice: str = Field(default="female_warm", max_length=40)
    style: str = Field(default="professional", max_length=40)


@app.post("/api/channel/clips")
def upload_clip(
    video: UploadFile = File(...),
    channel: str = Form(default="desktop"),
    sender: str = Form(default=""),
):
    try:
        clip = save_clip(video.filename or "source.mp4", video.file, channel=channel, sender=sender)
    except ChannelError as error:
        raise HTTPException(400, str(error)) from error
    megabytes = round(clip["size"] / 1024 / 1024, 1)
    return {
        "clip": clip,
        "hint": f"已收到视频（{megabytes} MB），请回复房源，例如：A区 中关村大厦 1201",
    }


@app.get("/api/channel/clips")
def get_clips(
    channel: str = "",
    status: str = "",
    limit: int = Query(default=20, ge=1, le=200),
):
    return {"items": list_clips(channel, status, limit)}


@app.get("/api/channel/resolve")
def resolve_property_query(q: str = Query(min_length=1, max_length=200), limit: int = Query(default=5, ge=1, le=20)):
    return resolve_channel_property(q.strip(), limit)


@app.post("/api/channel/clips/{clip_id}/bind")
def bind_clip_to_property(clip_id: str, payload: ClipBind):
    if payload.aspect_ratio not in {"9:16", "16:9"}:
        raise HTTPException(400, "当前视频工作台支持 9:16 或 16:9")
    try:
        return bind_clip(
            clip_id,
            query=payload.query,
            property_id=payload.property_id,
            target_duration=payload.target_duration,
            aspect_ratio=payload.aspect_ratio,
            voice=payload.voice,
            style=payload.style,
        )
    except ChannelError as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/channel/clips/{clip_id}")
def discard_clip(clip_id: str):
    if not drop_clip(clip_id):
        raise HTTPException(404, "视频不存在")
    return {"dropped": True, "clip_id": clip_id}


# ------------------------------------------------------------------ 分镜
# 抽帧交给人/模型看图，再按「一镜一句」回填，最后合成工作台要的文案。

class StoryboardShot(BaseModel):
    index: int = Field(ge=1, le=200)
    label: str = Field(default="", max_length=40)
    text: str = Field(default="", max_length=200)


class StoryboardPayload(BaseModel):
    shots: list[StoryboardShot]


@app.post("/api/tasks/{task_id}/analyze")
def analyze_task_shots(task_id: str, segment_seconds: float = Query(default=0, ge=0, le=60), force: bool = False):
    row = get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    if not row["source_video_path"]:
        raise HTTPException(400, "任务没有原始视频")
    try:
        return analyze(row["source_video_path"], row["task_no"], segment_seconds=segment_seconds, force=force)
    except ShotError as error:
        raise HTTPException(400, str(error)) from error


@app.get("/api/tasks/{task_id}/shot-frame/{index}")
def shot_frame(task_id: str, index: int):
    row = get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    try:
        path = frame_path(row["task_no"], index)
    except ShotError as error:
        raise HTTPException(404, str(error)) from error
    return FileResponse(path, media_type="image/jpeg")


@app.put("/api/tasks/{task_id}/storyboard")
def update_storyboard(task_id: str, payload: StoryboardPayload):
    try:
        return save_storyboard(task_id, [shot.model_dump() for shot in payload.shots])
    except ShotError as error:
        raise HTTPException(400, str(error)) from error


def serialize_template(row, detail: bool = False) -> dict:
    import json as _json

    item = {
        "id": row["id"],
        "name": row["name"],
        "source_type": row["source_type"],
        "source_url": row["source_url"],
        "duration_s": row["duration_s"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
    }
    if row["spec_json"]:
        try:
            spec = _json.loads(row["spec_json"])
        except ValueError:
            spec = {}
        item["summary"] = spec.get("summary", "")
        item["tone"] = spec.get("tone", "")
        item["shot_count"] = len(spec.get("shots") or [])
        if detail:
            item["spec"] = spec
            item["transcript"] = row["transcript"]
    return item


@app.get("/api/library/status")
def library_status():
    """视频库现状：能拆多少条、拆了多少、有没有建过向量。"""
    from .asr import probe_capabilities
    from .db import init_db
    from .semantic import is_configured, semantic_status

    init_db()
    with transaction() as connection:
        ready = connection.execute(
            "SELECT COUNT(*) AS n FROM style_templates WHERE status='ready'"
        ).fetchone()["n"]
        total = connection.execute("SELECT COUNT(*) AS n FROM style_templates").fetchone()["n"]
        indexed = connection.execute("SELECT COUNT(*) AS n FROM template_embeddings").fetchone()["n"]
    import app.library as library

    return {
        "templates": total,
        "ready": ready,
        "embedded": indexed,
        "semantic_configured": is_configured(),
        "semantic": semantic_status(),
        "capabilities": probe_capabilities(),
        "progress": library.PROGRESS,
    }


@app.post("/api/library/scan")
def library_scan(payload: dict):
    import app.library as library

    folder = str(payload.get("folder") or "").strip()
    if not folder:
        raise HTTPException(400, "请提供文件夹路径")
    try:
        files = library.scan(folder)
    except library.LibraryError as error:
        raise HTTPException(400, str(error)) from error
    return {"folder": folder, "count": len(files), "files": [path.name for path in files]}


@app.post("/api/library/ingest", status_code=202)
def library_ingest(payload: dict):
    import app.library as library

    folder = str(payload.get("folder") or "").strip()
    limit = int(payload.get("limit") or 0)
    if not folder:
        raise HTTPException(400, "请提供文件夹路径")
    try:
        return library.start_ingest(folder, limit=limit)
    except library.LibraryError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/library/reindex")
def library_reindex():
    import app.library as library

    return library.reindex_all()


@app.get("/api/templates/search")
def search_templates(q: str = Query(min_length=1), limit: int = Query(default=5, ge=1, le=20)):
    import app.library as library

    results = library.find_templates(q, limit=limit)
    with transaction() as connection:
        for item in results:
            row = connection.execute(
                "SELECT name, duration_s, spec_json FROM style_templates WHERE id=?", (item["template_id"],)
            ).fetchone()
            if row:
                import json as _json

                spec = _json.loads(row["spec_json"] or "{}")
                item["name"] = row["name"]
                item["duration_s"] = row["duration_s"]
                item["summary"] = spec.get("summary", "")
                item["shots"] = len(spec.get("shots") or [])
    return {"query": q, "items": results}


@app.get("/api/templates/suggest")
def suggest_templates(task_id: str = Query(min_length=1), limit: int = Query(default=3, ge=1, le=10)):
    """按任务对应的房源，推荐最像的历史片子。"""
    import app.library as library

    with transaction() as connection:
        task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(404, "任务不存在")
        prop = connection.execute("SELECT * FROM properties WHERE id=?", (task["property_id"],)).fetchone()
    if prop is None:
        raise HTTPException(404, "任务没有关联房源")
    return {"task_id": task_id, "items": library.suggest_for_property(prop, limit=limit)}


@app.get("/api/templates")
def list_templates(limit: int = Query(default=30, ge=1, le=200)):
    from .style_template import TEMPLATE_DIR, is_configured

    with transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM style_templates ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return {"items": [serialize_template(row) for row in rows], "configured": is_configured()}


@app.get("/api/templates/{template_id}")
def read_template(template_id: str):
    from .style_template import TEMPLATE_DIR

    with transaction() as connection:
        row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "模板不存在")
    return serialize_template(row, detail=True)


@app.post("/api/templates", status_code=202)
def create_template(video: UploadFile = File(...), name: str = Form(default="")):
    import uuid

    from .style_template import TEMPLATE_DIR, start_analysis

    template_id = uuid.uuid4().hex[:12]
    target_dir = TEMPLATE_DIR / template_id
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(video.filename or "source.mp4").suffix or ".mp4"
    target = target_dir / f"source{suffix}"
    with target.open("wb") as handle:
        shutil.copyfileobj(video.file, handle)

    if target.stat().st_size == 0:
        raise HTTPException(400, "上传的参考视频是空文件")

    now = utc_now()
    display = (name or Path(video.filename or "参考视频").stem)[:80]
    with transaction() as connection:
        connection.execute(
            """INSERT INTO style_templates(id, name, source_type, source_path, status, created_at, updated_at)
               VALUES (?, ?, 'upload', ?, 'pending', ?, ?)""",
            (template_id, display, str(target), now, now),
        )
    start_analysis(template_id)
    return {"id": template_id, "status": "pending"}


@app.post("/api/templates/from-url", status_code=202)
def create_template_from_url(payload: dict):
    import uuid

    from .style_template import StyleTemplateError, TEMPLATE_DIR, download_video, start_analysis

    url = str((payload or {}).get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "请填写 http/https 开头的视频链接")
    template_id = uuid.uuid4().hex[:12]
    try:
        path = download_video(url, TEMPLATE_DIR / template_id / "source.mp4")
    except StyleTemplateError as error:
        raise HTTPException(400, str(error)) from error

    now = utc_now()
    source_type = "douyin" if "douyin" in url or "iesdouyin" in url else "xiaohongshu" if "xiaohongshu" in url or "xhslink" in url else "upload"
    with transaction() as connection:
        connection.execute(
            """INSERT INTO style_templates(id, name, source_type, source_url, source_path, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (template_id, Path(path).stem[:80], source_type, url, str(path), now, now),
        )
    start_analysis(template_id)
    return {"id": template_id, "status": "pending"}


@app.delete("/api/templates/{template_id}")
def delete_template(template_id: str):
    from .style_template import TEMPLATE_DIR

    with transaction() as connection:
        row = connection.execute("SELECT id FROM style_templates WHERE id=?", (template_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "模板不存在")
        connection.execute("DELETE FROM style_templates WHERE id=?", (template_id,))
    shutil.rmtree(TEMPLATE_DIR / template_id, ignore_errors=True)
    return {"deleted": template_id}


@app.post("/api/tasks/{task_id}/apply-template")
def apply_template(task_id: str, payload: dict):
    from .style_template import StyleTemplateError, apply_to_task

    template_id = str((payload or {}).get("template_id") or "").strip()
    if not template_id:
        raise HTTPException(400, "请先选择模板")
    try:
        return apply_to_task(task_id, template_id)
    except StyleTemplateError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/style/render", status_code=202)
def style_render(payload: dict):
    """按模板 + 素材出一条成片。body:

    template_id / source 必填；storyboard 或 facts 二选一给文案来源；
    audio_mode ∈ mix(原声压低+口播) / original(只原声) / voice(只口播) / mute。
    """
    from .style_render import RenderError, start_render_job

    try:
        job = start_render_job(payload or {})
    except RenderError as error:
        raise HTTPException(400, str(error)) from error
    return job


@app.get("/api/style/render/{render_id}")
def style_render_status(render_id: str):
    from .style_render import RENDER_JOBS

    job = RENDER_JOBS.get(render_id)
    if job is None:
        raise HTTPException(404, "渲染任务不存在")
    return job


@app.get("/api/style/renders")
def style_renders():
    from .style_render import RENDER_JOBS

    return {"items": sorted(RENDER_JOBS.values(), key=lambda job: job["created_at"], reverse=True)}


@app.get("/api/bitable/status")
def bitable_status():
    from .feishu.bitable import bitable_client

    client = bitable_client()
    if client is None:
        return {"configured": False, "hint": "填写 FEISHU_APP_ID / FEISHU_APP_SECRET 与多维表格 App Token 后可用"}
    return {
        "configured": True,
        "task_table": client.config.task_table_id,
        "base_token_set": bool(client.config.base_app_token),
    }


@app.post("/api/tasks/{task_id}/sync-bitable")
def sync_task_bitable(task_id: str):
    from .feishu.bitable import bitable_client, sync_task as sync_bitable_task

    row = get_task(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    client = bitable_client()
    if client is None:
        raise HTTPException(400, "多维表格未配置")
    return {"result": sync_bitable_task(client, row), "task_no": row["task_no"]}


# 发布与审核的路由在 app/api/ 下按领域拆分，这里只做装配，见文件末尾的 include_router。


@app.get("/api/feishu/status")
def feishu_status():
    return get_bot().status()


@app.post("/api/feishu/restart")
def restart_feishu():
    bot = get_bot()
    bot.stop()
    started = bot.start()
    return {"started": started, **bot.status()}


@app.get("/api/feishu/events")
def feishu_events(limit: int = Query(default=30, ge=1, le=200)):
    with transaction() as connection:
        rows = connection.execute(
            """SELECT message_id, chat_id, msg_type, user_text, action, created_at
               FROM feishu_events ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
