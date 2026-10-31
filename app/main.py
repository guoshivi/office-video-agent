import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
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
    yield
    try:
        get_bot().stop()
    except Exception:
        pass


app = FastAPI(title="房源视频工作台", version=__version__, lifespan=lifespan)


class ScriptUpdate(BaseModel):
    script: str = Field(min_length=1, max_length=5000)


class StatusUpdate(BaseModel):
    status: str
    error_message: str = Field(default="", max_length=2000)


class SettingsUpdate(BaseModel):
    public_contact: str = Field(default="", max_length=300)
    default_duration: int = Field(default=45, ge=10, le=180)
    default_aspect_ratio: str = Field(default="9:16")
    default_voice: str = Field(default="female_warm")
    workbench_path: str = Field(default="", max_length=1000)
    workbench_url: str = Field(default="", max_length=500)
    workbench_output_root: str = Field(default="", max_length=1000)
    workbench_material_root: str = Field(default="", max_length=1000)


@app.get("/api/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "mode": "local",
        "database": "sqlite",
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
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


@app.post("/api/tasks")
async def create_task(
    property_id: str = Form(...),
    video: UploadFile = File(...),
    target_duration: int = Form(default=45),
    aspect_ratio: str = Form(default="9:16"),
    voice: str = Form(default="female_warm"),
    style: str = Form(default="professional"),
):
    if not 10 <= target_duration <= 180:
        raise HTTPException(400, "目标时长需在 10–180 秒之间")
    if aspect_ratio not in {"9:16", "16:9"}:
        raise HTTPException(400, "当前视频工作台支持 9:16 或 16:9")
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


@app.post("/api/tasks/{task_id}/export")
def export_task(task_id: str):
    try:
        return export_workbench_package(task_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/api/tasks/{task_id}/render", status_code=202)
def start_render(task_id: str, background_tasks: BackgroundTasks):
    settings = get_settings()
    if not settings.get("workbench_url"):
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
            """UPDATE tasks SET status='processing', stage='已排队，等待视频工作台',
               progress=1, error_message='', updated_at=? WHERE id=?""",
            (utc_now(), task_id),
        )
    background_tasks.add_task(render_task, task_id)
    return {"message": "已提交视频工作台，正在排队处理", "task": serialize_task(get_task(task_id))}


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
    if values["default_aspect_ratio"] not in {"9:16", "16:9"}:
        raise HTTPException(400, "当前视频工作台支持 9:16 或 16:9")
    if values["workbench_url"] and not values["workbench_url"].startswith(("http://", "https://")):
        raise HTTPException(400, "视频工作台 API 地址必须以 http:// 或 https:// 开头")
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
    target_duration: int = Field(default=45, ge=10, le=180)
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
