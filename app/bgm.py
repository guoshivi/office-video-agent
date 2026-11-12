"""内置 BGM 曲库：9 首本机合成的背景音乐，可商用、无第三方采样。

移植自 autovvvideo（视频工作台）的 workbench/music.py，只保留
「读清单 → 按 key 选曲 → 给出路径」这部分；混音在 style_render 的
filter_complex 里做。曲目与清单随包放在 data/render-env/music/。
"""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

MUSIC_DIR = Path(__file__).resolve().parent.parent / "data" / "render-env" / "music"
MANIFEST = MUSIC_DIR / "music.json"
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac"}


class MusicError(ValueError):
    pass


def _safe_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        return False
    return stat.S_ISREG(st.st_mode) and path.suffix.lower() in AUDIO_SUFFIXES


def _load() -> list[dict]:
    try:
        raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("tracks") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("file") or "").strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            continue
        path = (MUSIC_DIR / name).resolve()
        try:
            path.relative_to(MUSIC_DIR.resolve())
        except ValueError:
            continue
        if not _safe_file(path):
            continue
        out.append({
            "id": hashlib.sha1(name.encode("utf-8")).hexdigest()[:12],
            "title": str(row.get("title") or path.stem).strip()[:60],
            "mood": str(row.get("mood") or "其他").strip()[:12],
            "bpm": int(row.get("bpm") or 0),
            "duration": float(row.get("duration") or 0),
            "license": str(row.get("license") or "本机合成，可商用")[:80],
            "file": name,
            "path": str(path),
        })
    return out


def list_tracks() -> list[dict]:
    return _load()


def find(music_id: object) -> dict:
    if not isinstance(music_id, str) or not music_id:
        raise MusicError("曲目不存在")
    for row in _load():
        if row["id"] == music_id:
            return row
    raise MusicError("曲目不存在或文件已被移除")


def resolve(music_id: object) -> Path:
    row = find(music_id)
    path = (MUSIC_DIR / row["file"]).resolve()
    if not _safe_file(path):
        raise MusicError("曲目文件不可用")
    return path


def pick(seed: str, mood: str | None = None) -> dict | None:
    """按种子稳定选一曲：同一条任务永远同一首，不同任务有变化。

    mood 给了就在该情绪里选（选不到回退全部）；不给就在全部里选。
    曲库缺失返回 None，调用方静默跳过 BGM。
    """
    tracks = _load()
    if not tracks:
        return None
    if mood:
        subset = [t for t in tracks if t["mood"] == mood]
        tracks = subset or tracks
    digest = hashlib.sha256(str(seed or "").encode("utf-8")).digest()
    return tracks[digest[0] % len(tracks)]
