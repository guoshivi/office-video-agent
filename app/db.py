import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
_data_setting = Path(os.getenv("OVA_DATA_DIR", "data"))
DATA_DIR = (_data_setting if _data_setting.is_absolute() else BASE_DIR / _data_setting).resolve()
DB_PATH = DATA_DIR / "office_video_agent.db"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
FEISHU_DIR = DATA_DIR / "feishu"

_WRITE_LOCK = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS properties (
    id TEXT PRIMARY KEY,
    match_key TEXT NOT NULL DEFAULT '',
    region_raw TEXT NOT NULL DEFAULT '',
    building_raw TEXT NOT NULL DEFAULT '',
    room_raw TEXT NOT NULL DEFAULT '',
    region_norm TEXT NOT NULL DEFAULT '',
    building_norm TEXT NOT NULL DEFAULT '',
    room_norm TEXT NOT NULL DEFAULT '',
    area_raw TEXT NOT NULL DEFAULT '',
    area_sqm REAL,
    partition_raw TEXT NOT NULL DEFAULT '',
    rent_raw TEXT NOT NULL DEFAULT '',
    orientation_raw TEXT NOT NULL DEFAULT '',
    furnishing_layout_raw TEXT NOT NULL DEFAULT '',
    viewing_commission_raw TEXT NOT NULL DEFAULT '',
    contact_raw TEXT NOT NULL DEFAULT '',
    publish_date_raw TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    source_sheet TEXT NOT NULL DEFAULT '',
    source_row INTEGER NOT NULL DEFAULT 0,
    source_fingerprint TEXT UNIQUE,
    import_batch TEXT NOT NULL DEFAULT '',
    raw_row_json TEXT NOT NULL DEFAULT '{}',
    fill_color TEXT NOT NULL DEFAULT '',
    font_color TEXT NOT NULL DEFAULT '',
    is_new_marked INTEGER NOT NULL DEFAULT 0,
    is_special_marked INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_properties_match_key ON properties(match_key);
CREATE INDEX IF NOT EXISTS idx_properties_status ON properties(status);
CREATE INDEX IF NOT EXISTS idx_properties_batch ON properties(import_batch);

CREATE TABLE IF NOT EXISTS property_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL REFERENCES properties(id),
    import_batch TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    inserted_rows INTEGER NOT NULL DEFAULT 0,
    updated_rows INTEGER NOT NULL DEFAULT 0,
    skipped_rows INTEGER NOT NULL DEFAULT 0,
    review_rows INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    task_no TEXT NOT NULL UNIQUE,
    property_id TEXT NOT NULL REFERENCES properties(id),
    source TEXT NOT NULL DEFAULT 'webui',
    original_filename TEXT NOT NULL,
    source_video_path TEXT NOT NULL,
    target_duration INTEGER NOT NULL DEFAULT 45,
    aspect_ratio TEXT NOT NULL DEFAULT '9:16',
    voice TEXT NOT NULL DEFAULT 'female_warm',
    style TEXT NOT NULL DEFAULT 'professional',
    script TEXT NOT NULL DEFAULT '',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'script_ready',
    stage TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    workbench_package_path TEXT NOT NULL DEFAULT '',
    output_video_path TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_property ON tasks(property_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feishu_sessions (
    chat_id TEXT PRIMARY KEY,
    pending_message_id TEXT NOT NULL DEFAULT '',
    pending_video_path TEXT NOT NULL DEFAULT '',
    pending_filename TEXT NOT NULL DEFAULT '',
    pending_size INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feishu_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL DEFAULT '',
    chat_id TEXT NOT NULL DEFAULT '',
    msg_type TEXT NOT NULL DEFAULT '',
    user_text TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feishu_events_chat ON feishu_events(chat_id);

CREATE TABLE IF NOT EXISTS property_embeddings (
    property_id TEXT PRIMARY KEY REFERENCES properties(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON property_embeddings(model);

-- 各入口（WebUI / WorkBuddy 桌面 / IM）收到的原始视频先落在待绑定区，
-- 等房源确认后再建任务，这样“先发视频、后回房号”的对话节奏不会丢素材。
CREATE TABLE IF NOT EXISTS channel_clips (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL DEFAULT 'desktop',
    sender TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | bound | dropped
    task_id TEXT NOT NULL DEFAULT '',
    property_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_channel_clips_lookup
    ON channel_clips(channel, sender, status, created_at);

-- 每条成片在每个平台最多一条记录：重试走更新，不新建，
-- 避免同一条视频被重复推到同一个平台。
CREATE TABLE IF NOT EXISTS publish_records (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    platform TEXT NOT NULL,                   -- xiaohongshu | douyin
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|publishing|published|uncertain|failed
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    video_path TEXT NOT NULL DEFAULT '',
    remote_id TEXT NOT NULL DEFAULT '',
    remote_url TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_publish_records_task_platform
    ON publish_records(task_id, platform);
CREATE INDEX IF NOT EXISTS idx_publish_records_platform_time
    ON publish_records(platform, created_at);

-- 审核队列：出片后进入待审，人裁决后决定发还是重做。
-- 单独一张表而不是塞进 tasks.status，是因为「任务是否出片完成」和
-- 「这条片能不能对外发」是两个正交的维度，混在一起会让状态机爆炸。
CREATE TABLE IF NOT EXISTS review_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|published|skipped
    level TEXT NOT NULL DEFAULT 'ok',         -- ok|warn|blocking，预检总体结论
    precheck_json TEXT NOT NULL DEFAULT '{}',
    reviewer TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_items_status ON review_items(status, created_at);

-- 风格模板：从一条参考视频里拆出「脚本结构 + 文案口吻 + 分镜节奏」，
-- 之后套到别的素材上。只存结构和风格描述，不存参考片的画面。
CREATE TABLE IF NOT EXISTS style_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'upload',  -- upload | douyin | xiaohongshu
    source_url TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    duration_s REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | analyzing | ready | failed
    spec_json TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_style_templates_status ON style_templates(status, created_at);

-- 模板向量：让「这套房源该用哪条模板」可以按语义找，而不是靠人工挑。
CREATE TABLE IF NOT EXISTS template_embeddings (
    template_id TEXT PRIMARY KEY REFERENCES style_templates(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "public_contact": "欢迎私信咨询房源详情和看房安排",
    "default_duration": "45",
    "default_aspect_ratio": "9:16",
    "default_voice": "female_warm",
    "workbench_path": "",
    "workbench_url": os.getenv("WORKBENCH_URL", ""),
    "workbench_output_root": os.getenv(
        "WORKBENCH_OUTPUT_ROOT", str(DATA_DIR / "workbench_outputs")
    ),
    "workbench_material_root": os.getenv("WORKBENCH_MATERIAL_ROOT", ""),
    "public_base_url": os.getenv("OVA_PUBLIC_URL", ""),
    "feishu_auto_render": os.getenv("FEISHU_AUTO_RENDER", "0"),
    # 抽帧分镜用的 ffmpeg；留空则自动找 PATH 和工作台自带的那个
    "ffmpeg_path": os.getenv("FFMPEG_PATH", ""),
    "shot_segment_seconds": "6",
    # 出片引擎：本机是唯一主力，工作台只作为调试留口
    "render_engine": "local",
    "local_audio_mode": "mix",
    # 出片观感开关（花字对本机引擎与工作台引擎都生效）
    # 本机花字由 Pillow 本地画（app/flower_text.py），不存在模型挑错资源的问题，默认开。
    "render_sticker": os.getenv("OVA_RENDER_STICKER", "0"),
    "render_flower": os.getenv("OVA_RENDER_FLOWER", "1"),
    "render_sfx": os.getenv("OVA_RENDER_SFX", "0"),
    "render_sentence_gap": os.getenv("OVA_RENDER_SENTENCE_GAP", "0.22"),
    # 本机引擎的 BGM：曲库在 data/render-env/music（本机合成，可商用）
    "render_bgm": os.getenv("OVA_RENDER_BGM", "1"),
    "render_bgm_mood": os.getenv("OVA_RENDER_BGM_MOOD", ""),   # 留空=按任务稳定随机
    "render_bgm_volume": os.getenv("OVA_RENDER_BGM_VOLUME", "0.14"),
    # ---- 发布 ----
    # 两个平台默认都关：发布是破坏性动作，必须人工先确认账号和环境就绪再打开。
    "publish_xhs_enabled": os.getenv("PUBLISH_XHS_ENABLED", "0"),
    "publish_douyin_enabled": os.getenv("PUBLISH_DOUYIN_ENABLED", "0"),
    "publish_xhs_mcp_url": os.getenv("PUBLISH_XHS_MCP_URL", "http://127.0.0.1:18060/mcp"),
    "publish_xhs_token": os.getenv("PUBLISH_XHS_TOKEN", ""),
    "publish_douyin_cdp_url": os.getenv("PUBLISH_DOUYIN_CDP_URL", "http://127.0.0.1:9222"),
    # 中介号不是矩阵号，宁可少发也别进风控
    "publish_daily_limit": os.getenv("PUBLISH_DAILY_LIMIT", "3"),
    "publish_min_interval_minutes": os.getenv("PUBLISH_MIN_INTERVAL_MINUTES", "60"),
    # 审核通过后是否自动发；默认关，等审核台跑顺了再打开
    "publish_auto_after_review": os.getenv("PUBLISH_AUTO_AFTER_REVIEW", "0"),
    # 审核队列是否镜像到飞书多维表格并轮询人工裁决（需要 bitable 凭据）
    "review_bitable_sync": os.getenv("REVIEW_BITABLE_SYNC", "0"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    # 飞书长连接和 WebUI 会同时写库，给 SQLite 一点等待时间而不是直接报 locked
    connection.execute("PRAGMA busy_timeout=8000")
    return connection


@contextmanager
def transaction():
    """进程内串行化的数据库事务。

    飞书长连接、出片线程和 WebUI 会同时访问 SQLite。Windows 下 WAL 模式
    并发写偶发 "attempt to write a readonly database"，所以这里用一个可重入
    锁把写入串起来；访问量是单机中介场景，串行的代价可以忽略。
    """
    with _WRITE_LOCK:
        connection = connect()
        try:
            yield connection
            _commit_with_retry(connection)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _commit_with_retry(connection: sqlite3.Connection, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            connection.commit()
            return
        except sqlite3.OperationalError as error:
            if attempt == attempts - 1 or not _retryable(error):
                raise
            time.sleep(0.2 * (attempt + 1))


def _retryable(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "readonly" in message or "busy" in message


def _ensure_column(connection, table: str, column: str, definition: str) -> None:
    columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    for directory in (DATA_DIR, UPLOAD_DIR, EXPORT_DIR, FEISHU_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    with transaction() as connection:
        connection.executescript(SCHEMA)
        _ensure_column(connection, "tasks", "stage", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "tasks", "progress", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "tasks", "storyboard_json", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "tasks", "notify_chat_id", "TEXT NOT NULL DEFAULT ''")
        # 原声去留：空 = 跟设置里的默认值；mix/original/voice/mute = 这条任务单独指定
        _ensure_column(connection, "tasks", "audio_mode", "TEXT NOT NULL DEFAULT ''")
        for key, value in DEFAULT_SETTINGS.items():
            connection.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )


def get_settings() -> dict[str, str]:
    with transaction() as connection:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
