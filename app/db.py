import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("OVA_DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = DATA_DIR / "office_video_agent.db"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"

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
"""

DEFAULT_SETTINGS = {
    "public_contact": "欢迎私信咨询房源详情和看房安排",
    "default_duration": "45",
    "default_aspect_ratio": "9:16",
    "default_voice": "female_warm",
    "workbench_path": "",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def transaction():
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    for directory in (DATA_DIR, UPLOAD_DIR, EXPORT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    with transaction() as connection:
        connection.executescript(SCHEMA)
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
