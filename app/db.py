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
    "public_base_url": os.getenv("OVA_PUBLIC_URL", ""),
    "feishu_auto_render": os.getenv("FEISHU_AUTO_RENDER", "0"),
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
        _ensure_column(connection, "tasks", "notify_chat_id", "TEXT NOT NULL DEFAULT ''")
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
