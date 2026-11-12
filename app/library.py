"""视频库：把一个文件夹里的历史片子批量拆成模板，并建立向量索引。

一个中介手里真正值钱的不是某一条爆款，而是**几十条已经跑出过播放的成片**。
把它们放进同一个文件夹，让程序逐条拆解、编号、向量化，得到一件事：
给定一套新房源，能直接问「我最像哪一条老片子的打法」。

拆解本身在 refvideo 里（全本地，不联网）。这里只负责三件事：
1. 扫目录、去重、排队拆解；
2. 把拆好的结构写成一段可检索的文字，交给 embeddinggemma-2 编码；
3. 按相似度排序，挑出最适合套用的那几条。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path

from .db import init_db, json_dumps, transaction, utc_now
from .asr import _windows_path
from .refvideo import analyze_file

logger = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".flv"}


class LibraryError(RuntimeError):
    pass


# ------------------------------------------------------------------ 扫描

def scan(folder: str | Path, recursive: bool = True) -> list[Path]:
    """列出目录里的视频，按文件名排序（文件名里通常带日期，正好是时间顺序）。"""
    root = Path(_windows_path(folder))
    if not root.is_dir():
        raise LibraryError(f"目录不存在：{root}")
    pattern = "**/*" if recursive else "*"
    files = [
        path for path in root.glob(pattern)
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and not path.name.startswith(".")
    ]
    return sorted(files, key=lambda path: (path.parent.as_posix(), path.name))


def _existing_paths() -> dict[str, str]:
    with transaction() as connection:
        rows = connection.execute("SELECT id, source_path FROM style_templates").fetchall()
    return {str(Path(row["source_path"]).resolve()).lower(): row["id"] for row in rows if row["source_path"]}


# ------------------------------------------------------------------ 入库

def ingest_video(path: str | Path, name: str = "", force: bool = False) -> dict:
    """拆解一条视频并入库；已经拆过的直接返回，除非 force。

    返回值里带 status，调用方据此汇报进度，不必捕获异常。
    """
    init_db()
    path = Path(_windows_path(path)).resolve()
    key = str(path).lower()
    known = _existing_paths()
    if key in known and not force:
        with transaction() as connection:
            row = connection.execute(
                "SELECT id, status FROM style_templates WHERE id=?", (known[key],)
            ).fetchone()
        return {"path": str(path), "template_id": row["id"], "status": row["status"], "skipped": True}

    template_id = known.get(key) or f"tpl_{uuid.uuid4().hex[:12]}"
    label = name or _label_from_filename(path.name)
    now = utc_now()
    with transaction() as connection:
        connection.execute(
            """INSERT INTO style_templates(id, name, source_type, source_path, status, created_at, updated_at)
               VALUES (?, ?, 'library', ?, 'analyzing', ?, ?)
               ON CONFLICT(id) DO UPDATE SET status='analyzing', name=excluded.name, updated_at=excluded.updated_at""",
            (template_id, label, str(path), now, now),
        )

    try:
        spec = analyze_file(path)
    except Exception as error:  # noqa: BLE001 - 单条失败不该中断整库
        with transaction() as connection:
            connection.execute(
                "UPDATE style_templates SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(error)[:500], utc_now(), template_id),
            )
        logger.warning("视频库拆解失败 %s：%s", path.name, error)
        return {"path": str(path), "template_id": template_id, "status": "failed", "error": str(error)[:200]}

    transcript = "\n".join(shot["sample"] for shot in spec["shots"])
    with transaction() as connection:
        connection.execute(
            """UPDATE style_templates SET status='ready', spec_json=?, transcript=?, duration_s=?,
               error='', updated_at=? WHERE id=?""",
            (json_dumps(spec), transcript, spec["duration"], utc_now(), template_id),
        )
    indexed = _safe_embed(template_id, label, spec)
    return {
        "path": str(path), "template_id": template_id, "status": "ready",
        "name": label, "shots": len(spec["shots"]), "embedded": indexed,
    }


def ingest_folder(folder: str | Path, limit: int = 0, progress=None) -> dict:
    files = scan(folder)
    if limit:
        files = files[:limit]
    results = []
    seen: dict[str, str] = {}
    for index, file in enumerate(files, start=1):
        fingerprint = _fingerprint(file)
        twin = _content_twin(file)
        if twin:
            results.append({"path": str(file), "status": "duplicate", "duplicate_of": twin})
            if progress:
                progress(index, len(files), results[-1])
            continue
        if fingerprint in seen:
            results.append({
                "path": str(file), "status": "duplicate",
                "duplicate_of": seen[fingerprint],
            })
            if progress:
                progress(index, len(files), results[-1])
            continue
        seen[fingerprint] = str(file)
        result = ingest_video(file)
        results.append(result)
        if progress:
            progress(index, len(files), result)
    ready = [r for r in results if r["status"] == "ready"]
    return {
        "total": len(files),
        "ready": len(ready),
        "failed": len([r for r in results if r["status"] == "failed"]),
        "duplicates": len([r for r in results if r["status"] == "duplicate"]),
        "items": results,
    }


def _content_twin(path: Path) -> str:
    """同一条片子导出两次，字节不同但时长和切点几乎一样，认作同一条。"""
    import json as _json

    try:
        from .refvideo import scene_cuts
        from .shots import probe_duration

        duration = probe_duration(path)
        cuts = scene_cuts(path)
    except Exception:  # noqa: BLE001 - 判重失败就当它不重复
        return ""
    with transaction() as connection:
        rows = connection.execute(
            "SELECT id, name, duration_s, spec_json FROM style_templates WHERE status='ready'"
        ).fetchall()
    for row in rows:
        if abs((row["duration_s"] or 0) - duration) > 0.8:
            continue
        try:
            other = _json.loads(row["spec_json"] or "{}").get("cuts") or []
        except Exception:  # noqa: BLE001
            continue
        if len(other) == len(cuts) and all(abs(a - b) <= 0.3 for a, b in zip(other, cuts)):
            return row["name"] or row["id"]
    return ""


def _content_twin(path: Path) -> str:
    """同一条片子导出两次，字节不同但时长和切点几乎一样，认作同一条。"""
    import json as _json

    try:
        from .refvideo import scene_cuts
        from .shots import probe_duration

        duration = probe_duration(path)
        cuts = scene_cuts(path)
    except Exception:  # noqa: BLE001 - 判重失败就当它不重复
        return ""
    with transaction() as connection:
        rows = connection.execute(
            "SELECT id, name, duration_s, spec_json FROM style_templates WHERE status='ready'"
        ).fetchall()
    for row in rows:
        if abs((row["duration_s"] or 0) - duration) > 0.8:
            continue
        try:
            other = _json.loads(row["spec_json"] or "{}").get("cuts") or []
        except Exception:  # noqa: BLE001
            continue
        if len(other) == len(cuts) and all(abs(a - b) <= 0.3 for a, b in zip(other, cuts)):
            return row["name"] or row["id"]
    return ""


def _fingerprint(path: Path) -> str:
    """按「文件大小 + 前 256KB」判重。

    同一片子导出两次，文件名不同、字节完全相同；
    只比大小不够（同长度不同内容），全量哈希又太慢，取头部足够。
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(262144)
    return hashlib.sha1(f"{size}:".encode("utf-8") + head).hexdigest()


def _label_from_filename(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"[_\-]+", " ", stem).strip() or name


# ------------------------------------------------------------------ 向量

def template_text(name: str, spec: dict) -> str:
    """拼出用于检索的文字。

    只放「这条片子是什么打法」的信息：结构、每句在说什么、角标写了什么。
    不放入片长之类的数字，否则会干扰语义相似度。
    """
    parts = [name]
    roles = "、".join(f"{item['role']}{item['seconds']:.0f}秒" for item in spec.get("structure") or [])
    if roles:
        parts.append(f"结构：{roles}")
    pacing = spec.get("pacing") or {}
    if pacing.get("cut_count"):
        parts.append(
            f"节奏：{pacing['cut_count'] + 1} 个镜头，平均 {pacing.get('avg_cut_seconds', 0)} 秒切一次"
        )
    pacing = spec.get("pacing") or {}
    if pacing.get("cut_count"):
        parts.append(
            f"节奏：{pacing['cut_count'] + 1} 个镜头，平均 {pacing.get('avg_cut_seconds', 0)} 秒切一次"
        )
    lines = " ".join(str(shot.get("sample") or "") for shot in spec.get("shots") or [])
    if lines:
        parts.append(f"文案：{lines}")
    labels = " ".join(str(item.get("text") or "") for item in spec.get("labels") or [])
    if labels:
        parts.append(f"角标：{labels}")
    return "\n".join(parts)


def _safe_embed(template_id: str, name: str, spec: dict) -> bool:
    """建向量失败不影响拆解结果——检索可以退化成按名字挑。"""
    try:
        return embed_template(template_id, name, spec)
    except Exception as error:  # noqa: BLE001
        logger.warning("模板向量化失败 %s：%s", template_id, error)
        return False


def embed_template(template_id: str, name: str = "", spec: dict | None = None) -> bool:
    from .semantic import embed_texts, is_configured, semantic_config

    if not is_configured():
        return False
    if spec is None:
        with transaction() as connection:
            row = connection.execute(
                "SELECT name, spec_json FROM style_templates WHERE id=?", (template_id,)
            ).fetchone()
        if row is None:
            raise LibraryError("模板不存在")
        name = row["name"]
        import json as _json

        spec = _json.loads(row["spec_json"] or "{}")

    text = template_text(name, spec)
    vectors = asyncio.run(embed_texts([text]))
    vector = vectors[0]
    config = semantic_config()
    with transaction() as connection:
        connection.execute(
            """INSERT INTO template_embeddings(template_id, model, content_hash, vector_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(template_id) DO UPDATE SET
                 model=excluded.model, content_hash=excluded.content_hash,
                 vector_json=excluded.vector_json, updated_at=excluded.updated_at""",
            (
                template_id,
                config["model"],
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
                json_dumps(vector),
                utc_now(),
            ),
        )
    return True


def reindex_all() -> dict:
    init_db()
    with transaction() as connection:
        rows = connection.execute("SELECT id, name, spec_json FROM style_templates WHERE status='ready'").fetchall()
    import json as _json

    done = 0
    for row in rows:
        try:
            if embed_template(row["id"], row["name"], _json.loads(row["spec_json"] or "{}")):
                done += 1
        except Exception as error:  # noqa: BLE001
            logger.warning("重新向量化失败 %s：%s", row["id"], error)
    return {"total": len(rows), "embedded": done}


# ------------------------------------------------------------------ 后台任务

PROGRESS: dict = {
    "running": False,
    "folder": "",
    "done": 0,
    "total": 0,
    "current": "",
    "results": [],
    "finished_at": "",
}


def start_ingest(folder: str | Path, limit: int = 0) -> dict:
    """在后台跑整库拆解。一个文件夹一次，重复调用会被忽略。"""
    import threading

    if PROGRESS["running"]:
        return {"started": False, "reason": "已经有一批在跑", **PROGRESS}

    files = scan(folder)
    PROGRESS.update({
        "running": True, "folder": str(folder), "done": 0, "total": len(files) if not limit else min(limit, len(files)),
        "current": "", "results": [], "finished_at": "",
    })

    def worker():
        def report(index, total, result):
            PROGRESS["done"] = index
            PROGRESS["current"] = Path(result["path"]).name
            PROGRESS["results"].append({k: v for k, v in result.items() if k != "path"} | {"file": Path(result["path"]).name})

        try:
            summary = ingest_folder(folder, limit=limit, progress=report)
            PROGRESS["summary"] = summary
        except Exception as error:  # noqa: BLE001
            PROGRESS["summary"] = {"error": str(error)[:300]}
        finally:
            PROGRESS["running"] = False
            PROGRESS["current"] = ""
            PROGRESS["finished_at"] = utc_now()

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, **PROGRESS}


# ------------------------------------------------------------------ 检索

def _load_vectors() -> list[tuple[str, list[float], str]]:
    with transaction() as connection:
        rows = connection.execute(
            """SELECT e.template_id, e.vector_json, t.name
               FROM template_embeddings e JOIN style_templates t ON t.id = e.template_id
               WHERE t.status = 'ready'"""
        ).fetchall()
    import json as _json

    return [(row["template_id"], _json.loads(row["vector_json"]), row["name"]) for row in rows]


def find_templates(query: str, limit: int = 5) -> list[dict]:
    """按语义找最接近的模板。没有向量索引时退化成按名字包含匹配。"""
    from .semantic import cosine, embed_texts, is_configured

    init_db()

    catalog = _load_vectors()
    if not catalog or not is_configured():
        return _fallback_search(query, limit)

    query_vector = asyncio.run(embed_texts([query]))[0]
    scored = [
        {"template_id": template_id, "name": name, "score": round(cosine(query_vector, vector), 4)}
        for template_id, vector, name in catalog
    ]
    scored.sort(key=lambda item: -item["score"])
    return scored[:limit]


def _fallback_search(query: str, limit: int) -> list[dict]:
    with transaction() as connection:
        rows = connection.execute(
            "SELECT id, name, spec_json FROM style_templates WHERE status='ready' ORDER BY created_at DESC"
        ).fetchall()
    words = [word for word in re.split(r"[\s，,。、]+", query) if len(word) >= 2]
    scored = []
    for row in rows:
        text = f"{row['name']} {row['spec_json']}"
        hits = sum(1 for word in words if word in text)
        scored.append({"template_id": row["id"], "name": row["name"], "score": round(hits / max(len(words), 1), 2)})
    scored.sort(key=lambda item: -item["score"])
    return scored[:limit]


def suggest_for_property(property_row, limit: int = 3) -> list[dict]:
    """给一套房源挑模板：用它的面积、格局、朝向、家具这些事实去问。"""
    fields = []
    for key in ("area", "layout", "toward", "furniture", "building", "zone", "rent", "remark"):
        try:
            value = property_row[key]
        except (KeyError, IndexError):
            continue
        if value not in (None, ""):
            fields.append(str(value))
    query = "办公室租赁短视频，房源情况：" + "，".join(fields)
    return find_templates(query, limit=limit)
