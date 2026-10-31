import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .db import DATA_DIR, UPLOAD_DIR, get_settings, init_db, json_dumps, transaction, utc_now
from .excel_import import import_excel
from .services import (
    export_workbench_package,
    generate_script,
    get_task,
    new_task_no,
    public_property,
    serialize_task,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_EXCEL_SIZE = 20 * 1024 * 1024
MAX_VIDEO_SIZE = 2 * 1024 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
TASK_STATUSES = {"received", "script_ready", "exported", "processing", "review", "completed", "failed"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


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


@app.get("/api/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "mode": "local",
        "database": "sqlite",
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "workbench_configured": bool(settings.get("workbench_path")),
        "semantic_search_configured": False,
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
    if aspect_ratio not in {"9:16", "16:9", "1:1"}:
        raise HTTPException(400, "不支持的视频比例")
    with transaction() as connection:
        property_row = connection.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
    if property_row is None:
        raise HTTPException(404, "请选择有效房源")

    original_filename = Path(video.filename or "source.mp4").name
    extension = Path(original_filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        raise HTTPException(400, "视频格式支持 MP4、MOV、M4V、AVI、MKV")

    task_id = str(uuid.uuid4())
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
        script, warnings = generate_script(property_row)
        now = utc_now()
        with transaction() as connection:
            connection.execute(
                """INSERT INTO tasks(
                    id, task_no, property_id, source, original_filename, source_video_path,
                    target_duration, aspect_ratio, voice, style, script, warnings_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, 'webui', ?, ?, ?, ?, ?, ?, ?, ?, 'script_ready', ?, ?)""",
                (
                    task_id, task_no, property_id, original_filename, str(video_path),
                    target_duration, aspect_ratio, voice, style, script,
                    json_dumps(warnings), now, now,
                ),
            )
    except Exception:
        video_path.unlink(missing_ok=True)
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


@app.put("/api/tasks/{task_id}/script")
def update_script(task_id: str, payload: ScriptUpdate):
    if get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    with transaction() as connection:
        connection.execute(
            """UPDATE tasks SET script=?, status='script_ready',
               workbench_package_path='', updated_at=? WHERE id=?""",
            (payload.script.strip(), utc_now(), task_id),
        )
    return serialize_task(get_task(task_id))


@app.post("/api/tasks/{task_id}/export")
def export_task(task_id: str):
    try:
        return export_workbench_package(task_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


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
    if values["default_aspect_ratio"] not in {"9:16", "16:9", "1:1"}:
        raise HTTPException(400, "不支持的默认视频比例")
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
        "note": "数据库、上传视频和导出任务包均保存在本地；迁移时需要备份整个 data 目录。",
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
