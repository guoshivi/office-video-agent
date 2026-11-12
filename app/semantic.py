import asyncio
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx

from .db import DATA_DIR, json_dumps, transaction, utc_now
from .services import public_property, safe_public_text

QUERY_CACHE: dict[tuple[str, str], list[float]] = {}
logger = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent.parent
_LOCAL_MODEL_LOCK = threading.Lock()
_LOCAL_MODELS: dict[str, object] = {}


def _local_model_dir(config: dict[str, str]) -> Path:
    """本地模型目录：优先 SEMANTIC_MODEL_DIR，否则 models/<模型名>。"""
    raw = config["model_dir"] or str(BASE_DIR / "models" / (config["model"] or "embeddinggemma-2"))
    path = Path(raw)
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


def _embed_local(texts: list[str]) -> list[list[float]]:
    """进程内直接跑本地向量模型（sentence-transformers）。

    和 workbench_cli 的区别：不依赖工作台上的私有 videopipe 包，
    所以工作台在另一台机器上时也能用。只加载文本能力，省显存和内存。
    """
    config = semantic_config()
    model_dir = _local_model_dir(config)
    if not (model_dir / "config.json").is_file():
        raise RuntimeError(f"本地向量模型目录不完整：{model_dir}")
    key = str(model_dir)
    with _LOCAL_MODEL_LOCK:
        model = _LOCAL_MODELS.get(key)
        if model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            use_cuda = torch.cuda.is_available()
            model = SentenceTransformer(
                key,
                device="cuda" if use_cuda else "cpu",
                config_kwargs={"vision_config": None, "audio_config": None},
                # 有显卡用 fp16（3060 上快一个数量级）；CPU 上 bf16 比 fp32 慢约 6 倍
                model_kwargs={"dtype": torch.float16 if use_cuda else torch.float32},
            )
            _LOCAL_MODELS[key] = model
            logger.info("本地向量模型已加载：%s（device=%s）", key, "cuda" if use_cuda else "cpu")
    batch_size = 32 if model.device.type == "cuda" else 16
    vectors = model.encode(
        texts,
        prompt_name="SearchQuery",
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vector.tolist() for vector in vectors]


def semantic_config() -> dict[str, str]:
    return {
        "provider": os.getenv("SEMANTIC_PROVIDER", "http").strip().lower() or "http",
        "base_url": os.getenv("SEMANTIC_BASE_URL", "").strip().rstrip("/"),
        "model": os.getenv("SEMANTIC_MODEL", "").strip(),
        "api_key": os.getenv("SEMANTIC_API_KEY", "").strip(),
        "path": os.getenv("SEMANTIC_EMBEDDINGS_PATH", "/embeddings").strip() or "/embeddings",
        "workbench_python": os.getenv("SEMANTIC_WORKBENCH_PYTHON", "").strip(),
        "workbench_root": os.getenv("SEMANTIC_WORKBENCH_ROOT", "").strip(),
        "model_dir": os.getenv("SEMANTIC_MODEL_DIR", "").strip(),
        "gpu_lock_path": os.getenv("SEMANTIC_GPU_LOCK_PATH", "").strip(),
    }


def is_configured() -> bool:
    config = semantic_config()
    if config["provider"] == "workbench_cli":
        return bool(
            config["workbench_python"]
            and Path(config["workbench_python"]).is_file()
            and config["workbench_root"]
            and Path(config["workbench_root"]).is_dir()
            and config["model_dir"]
            and Path(config["model_dir"]).is_dir()
        )
    if config["provider"] == "local":
        model_dir = _local_model_dir(config)
        return bool(config["model"] and model_dir.is_dir() and (model_dir / "config.json").is_file())
    return bool(config["base_url"] and config["model"])


def semantic_status() -> dict:
    config = semantic_config()
    with transaction() as connection:
        property_count = connection.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        indexed_count = connection.execute(
            "SELECT COUNT(*) FROM property_embeddings WHERE model=?", (config["model"],)
        ).fetchone()[0] if config["model"] else 0
    return {
        "configured": is_configured(),
        "provider": config["provider"],
        "model": config["model"],
        "base_url": config["base_url"] if config["provider"] == "http" else "",
        "property_count": property_count,
        "indexed_count": indexed_count,
        "index_complete": bool(config["model"]) and indexed_count >= property_count,
    }


def embedding_text(row) -> str:
    public = public_property(row)
    fields = [
        ("区域", public["region"]),
        ("楼栋", public["building"]),
        ("房号", public["room"]),
        ("面积", public["area"]),
        ("格局", public["partition"]),
        ("朝向", public["orientation"]),
        ("房源特点", safe_public_text(public["furnishing_layout"], 80)),
        ("租金", safe_public_text(public["rent"], 40)),
    ]
    return "；".join(f"{label}：{value}" for label, value in fields if value)


def _embed_with_workbench_cli(texts: list[str]) -> list[list[float]]:
    config = semantic_config()
    if not is_configured():
        raise RuntimeError("视频工作台向量模型路径尚未配置完整")
    tool = Path(__file__).resolve().parent.parent / "tools" / "workbench_embed_cli.py"
    temporary_root = DATA_DIR / "semantic_tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="embed-", dir=temporary_root) as temp_dir:
        input_path = Path(temp_dir) / "input.json"
        output_path = Path(temp_dir) / "output.json"
        request = {
            "texts": texts,
            "model": config["model"] or Path(config["model_dir"]).name,
            "workbench_root": config["workbench_root"],
            "model_dir": config["model_dir"],
            "gpu_lock_path": config["gpu_lock_path"],
            "prompt_name": "SearchQuery",
            "batch_size": 16,
        }
        input_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        try:
            completed = subprocess.run(
                [config["workbench_python"], str(tool), str(input_path), str(output_path)],
                capture_output=True,
                text=True,
                timeout=1800,
                cwd=config["workbench_root"],
                env={**os.environ, "PYTHONUTF8": "1"},
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("视频工作台向量模型加载或推理超时") from error
        if output_path.is_file():
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            vectors = payload.get("vectors") or []
            if len(vectors) == len(texts):
                return vectors
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise RuntimeError(f"视频工作台向量模型执行失败：{detail or '没有返回向量'}")


async def embed_texts(texts: list[str]) -> list[list[float]]:
    config = semantic_config()
    if not texts:
        return []
    if config["provider"] == "workbench_cli":
        return await asyncio.to_thread(_embed_with_workbench_cli, texts)
    if config["provider"] == "local":
        return await asyncio.to_thread(_embed_local, texts)
    if not config["base_url"] or not config["model"]:
        raise RuntimeError("本地向量模型尚未配置")
    headers = {"Content-Type": "application/json"}
    if config["api_key"]:
        headers["Authorization"] = f"Bearer {config['api_key']}"
    url = config["base_url"] + "/" + config["path"].lstrip("/")
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                url,
                headers=headers,
                json={"model": config["model"], "input": texts},
            )
    except httpx.HTTPError as error:
        raise RuntimeError(f"无法连接本地向量服务：{error}") from error
    if response.status_code >= 400:
        raise RuntimeError(f"向量服务返回 {response.status_code}：{response.text[:300]}")
    try:
        payload = response.json()
        data = sorted(payload["data"], key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in data]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("向量服务返回格式不是 OpenAI 兼容的 embeddings 响应") from error
    if len(vectors) != len(texts) or any(not vector for vector in vectors):
        raise RuntimeError("向量服务返回的向量数量或内容不完整")
    if len({len(vector) for vector in vectors}) != 1:
        raise RuntimeError("向量服务返回了不同维度的向量")
    return vectors


async def reindex_properties() -> dict:
    config = semantic_config()
    if not is_configured():
        raise RuntimeError("请先在 .env 中配置 SEMANTIC_BASE_URL 和 SEMANTIC_MODEL")
    with transaction() as connection:
        rows = connection.execute("SELECT * FROM properties ORDER BY source_sheet, source_row").fetchall()
    indexed = 0
    texts = [embedding_text(row) for row in rows]
    if config["provider"] == "workbench_cli":
        vectors = await embed_texts(texts)
        grouped = [(rows, texts, vectors)]
    else:
        batch_size = 32
        grouped = []
        for offset in range(0, len(rows), batch_size):
            batch_rows = rows[offset:offset + batch_size]
            batch_texts = texts[offset:offset + batch_size]
            grouped.append((batch_rows, batch_texts, await embed_texts(batch_texts)))

    for batch_rows, batch_texts, batch_vectors in grouped:
        with transaction() as connection:
            for row, text_value, vector in zip(batch_rows, batch_texts, batch_vectors):
                connection.execute(
                    """INSERT INTO property_embeddings(property_id, model, content_hash, vector_json, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(property_id) DO UPDATE SET
                         model=excluded.model,
                         content_hash=excluded.content_hash,
                         vector_json=excluded.vector_json,
                         updated_at=excluded.updated_at""",
                    (
                        row["id"],
                        config["model"],
                        hashlib.sha256(text_value.encode("utf-8")).hexdigest(),
                        json_dumps(vector),
                        utc_now(),
                    ),
                )
        indexed += len(batch_rows)
    return {"indexed_count": indexed, "model": config["model"], **semantic_status()}


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def hybrid_adjustment(query: str, row) -> float:
    """Correct semantic similarity with explicit constraints users expect."""
    adjustment = 0.0
    public = public_property(row)
    area_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:平|平米|平方米|㎡)", query)
    if area_match and public["area_sqm"]:
        target = float(area_match.group(1))
        adjustment += 0.12 * math.exp(-abs(public["area_sqm"] - target) / max(35.0, target * 0.35))
    region_match = re.search(r"([A-Za-z0-9一二三四五六七八九十]+区)", query, re.I)
    if region_match:
        wanted = region_match.group(1).upper()
        adjustment += 0.10 if wanted in public["region"].upper() else -0.10
    orientation_match = re.search(r"朝[东南西北]{1,2}", query)
    if orientation_match and public["orientation"]:
        adjustment += 0.05 if orientation_match.group(0) in public["orientation"] else -0.02
    public_text = " ".join(
        str(public.get(key) or "")
        for key in ("partition", "furnishing_layout", "orientation")
    )
    for feature in ("家具", "工位", "阳台", "电梯", "隔间", "采光"):
        if feature in query and feature in public_text:
            adjustment += 0.025
    return adjustment


async def search_properties(query: str, limit: int = 30) -> list[dict]:
    config = semantic_config()
    if not is_configured():
        raise RuntimeError("本地向量模型尚未配置")
    cache_key = (config["model"], query)
    vector = QUERY_CACHE.get(cache_key)
    if vector is None:
        vector = (await embed_texts([query]))[0]
        if len(QUERY_CACHE) >= 128:
            QUERY_CACHE.pop(next(iter(QUERY_CACHE)))
        QUERY_CACHE[cache_key] = vector
    with transaction() as connection:
        rows = connection.execute(
            """SELECT p.*, e.vector_json FROM property_embeddings e
               JOIN properties p ON p.id=e.property_id
               WHERE e.model=?""",
            (config["model"],),
        ).fetchall()
    if not rows:
        raise RuntimeError("房源向量索引为空，请先在系统设置中建立索引")
    ranked = []
    for row in rows:
        try:
            semantic_score = cosine(vector, json.loads(row["vector_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        score = max(-1.0, min(1.0, semantic_score + hybrid_adjustment(query, row)))
        ranked.append((score, semantic_score, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [
        {**public_property(row), "semantic_score": round(semantic_score, 4), "score": round(score, 4)}
        for score, semantic_score, row in ranked[:limit]
    ]
