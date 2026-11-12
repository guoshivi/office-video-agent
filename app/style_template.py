"""风格模板：把一条参考视频拆成「脚本结构 + 文案口吻 + 分镜节奏」，再套到别的素材上。

分工：
- ffmpeg / ffprobe 负责本地探测（复用 shots.py 的路径解析，不重复造轮子）
- 火山方舟负责「看懂视频」和「写文案」：Files API 传本地文件拿 file_id，
  Chat API 里用 video_url 引用，一个 ARK_API_KEY 走完
- 本模块只产出**结构和文案**，真正的渲染还是交给视频工作台

刻意不做的事：不存参考片画面、不保存原视频到成片流程里，
避免"搬运"的版权问题——模板只描述结构，套用时文案必须来自房源字段。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

import httpx

from .db import DATA_DIR, json_dumps, transaction, utc_now
from .services import get_task, public_property
from .shots import (
    MAX_SHOT_TEXT,
    ShotError,
    _find_binary,
    analyze as analyze_shots,
    load_manifest,
    probe_duration,
    save_storyboard,
)

logger = logging.getLogger(__name__)

TEMPLATE_DIR = DATA_DIR / "templates"
DEFAULT_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
MAX_VIDEO_BYTES = 512 * 1024 * 1024  # 方舟默认托管存储单文件上限

ANALYZE_PROMPT = """你是短视频结构分析师。请拆解这条参考视频，输出可复用的「脚本模板」。

要求：
1. 只描述**结构和风格**，不要照抄具体的房源事实（面积、租金、地址这类必须留空）。
2. 口播文案尽量逐句转写；听不清就写你能确认的部分，不要编。
3. 句数按视频里实际换镜/换句的节奏来，最多 8 句。

只输出 JSON，不要任何解释，格式：
{
  "summary": "一句话概括这条片子怎么抓人",
  "tone": "口吻描述，例如：口语、短句、第二人称，开头用数字钩子",
  "structure": [{"role": "hook|痛点|亮点|位置|面积|价格|cta", "share": 0.15}],
  "shots": [{"index": 1, "role": "hook", "seconds": 4.0, "sample": "这一句的原话", "words": 9}],
  "constraints": {"max_words_per_shot": 20, "total_seconds": 30},
  "tips": ["可复用的技巧，例如：前3秒必须出现具体数字"]
}"""

APPLY_PROMPT = """你在给一条办公室租赁短视频写口播文案。

参考模板（来自另一条片子，只学结构和口吻，不要抄内容）：
{spec}

这套房源的可用信息（只能用这里出现的，不许编）：
{facts}

要求：
1. 输出 {count} 句，一镜一句，顺序跟模板的 shots 一致。
2. 每句不超过 {limit} 字，越短越好；中文口播每秒约 4 字。
3. 口吻贴近模板的 tone，但内容必须换成这套房源的真实信息。
4. 信息不够的地方就少说，绝不虚构面积、租金、朝向、配套。

只输出 JSON：{{"shots": [{{"index": 1, "role": "hook", "text": "第一句"}}]}}"""


class StyleTemplateError(RuntimeError):
    pass


# ------------------------------------------------------------------ 配置

def style_config() -> dict:
    model = os.getenv("STYLE_VISION_MODEL", "").strip() or "doubao-seed-2-1-pro-260915"
    return {
        "api_key": os.getenv("ARK_API_KEY", "").strip(),
        "vision_model": model,
        "text_model": os.getenv("STYLE_TEXT_MODEL", "").strip() or model,
        "fps": os.getenv("STYLE_VIDEO_FPS", "0.5").strip() or "0.5",
    }


def is_configured() -> bool:
    return bool(style_config()["api_key"])


def _client(timeout: float = 600) -> httpx.Client:
    config = style_config()
    if not config["api_key"]:
        raise StyleTemplateError("尚未配置 ARK_API_KEY，风格模板功能不可用")
    base = (os.getenv("ARK_BASE_URL", "").strip() or DEFAULT_ARK_BASE).rstrip("/")
    return httpx.Client(
        base_url=base,
        timeout=httpx.Timeout(timeout, connect=15),
        headers={"Authorization": f"Bearer {config['api_key']}"},
        trust_env=False,
    )


# ------------------------------------------------------------------ 方舟调用

def upload_video(path: Path) -> str:
    """用 Files API 传本地视频，返回 file_id（方舟侧存 7 天）。"""
    config = style_config()
    size = path.stat().st_size
    if size > MAX_VIDEO_BYTES:
        raise StyleTemplateError(f"参考视频 {size / 1048576:.0f}MB 超过方舟 512MB 上限，请先压缩")
    data = {
        "purpose": "user_data",
        "preprocess_configs[video][model]": config["vision_model"],
        "preprocess_configs[video][fps]": str(config["fps"]),
    }
    with _client() as client, path.open("rb") as handle:
        response = client.post(
            "/files",
            data=data,
            files={"file": (path.name, handle, "video/mp4")},
        )
        if response.status_code >= 400:
            raise StyleTemplateError(f"上传视频失败 {response.status_code}：{response.text[:300]}")
        payload = response.json()
    file_id = payload.get("id")
    if not file_id:
        raise StyleTemplateError(f"上传视频没有返回 file_id：{payload}")
    return file_id


def wait_for_file(file_id: str, timeout: float = 180) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with _client(60) as client:
            response = client.get(f"/files/{file_id}")
        if response.status_code >= 400:
            raise StyleTemplateError(f"查询文件状态失败 {response.status_code}：{response.text[:200]}")
        status = (response.json() or {}).get("status")
        if status == "active":
            return
        if status in {"failed", "error"}:
            raise StyleTemplateError(f"方舟处理视频失败（status={status}）")
        time.sleep(3)
    raise StyleTemplateError("等待方舟处理视频超时（3 分钟）")


def _extract_json(text: str) -> dict:
    """模型偶尔会在 JSON 外面裹代码块，这里兜一下。"""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise StyleTemplateError(f"模型返回的不是 JSON：{text[:200]}")


def _chat(messages: list[dict], model: str, *, json_mode: bool = True) -> dict:
    body = {"model": model, "messages": messages, "temperature": 0.3}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    with _client() as client:
        response = client.post("/chat/completions", json=body)
        if response.status_code == 400 and json_mode:
            body.pop("response_format", None)
            response = client.post("/chat/completions", json=body)
        if response.status_code >= 400:
            raise StyleTemplateError(f"模型调用失败 {response.status_code}：{response.text[:300]}")
        payload = response.json()
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise StyleTemplateError(f"模型返回结构异常：{str(payload)[:300]}") from error
    return _extract_json(content)


# ------------------------------------------------------------------ 模板拆解

def _spec_summary(spec: dict) -> str:
    shots = spec.get("shots") or []
    return " / ".join(str(shot.get("role") or "") for shot in shots[:8])


def analyze(template_id: str) -> dict:
    """后台线程里跑：上传 → 拆解 → 落库。"""
    with transaction() as connection:
        row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise StyleTemplateError("模板不存在")

    try:
        path = Path(row["source_path"])
        if not path.is_file():
            raise StyleTemplateError("参考视频文件已丢失，请重新上传")

        duration = probe_duration(path)
        file_id = upload_video(path)
        wait_for_file(file_id)

        config = style_config()
        messages = [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"file_id": file_id}},
                {"type": "text", "text": ANALYZE_PROMPT},
            ],
        }]
        spec = _chat(messages, config["vision_model"])

        transcript = "\n".join(
            str(shot.get("sample") or "") for shot in (spec.get("shots") or []) if shot.get("sample")
        )
        with transaction() as connection:
            connection.execute(
                """UPDATE style_templates SET status='ready', spec_json=?, transcript=?,
                   duration_s=?, error='', updated_at=? WHERE id=?""",
                (json_dumps(spec), transcript, duration, utc_now(), template_id),
            )
        logger.info("风格模板拆解完成：%s（%s）", template_id, _spec_summary(spec))
        return spec
    except Exception as error:  # noqa: BLE001 - 后台线程必须把失败写回库
        logger.warning("风格模板拆解失败 %s：%s", template_id, error)
        with transaction() as connection:
            connection.execute(
                "UPDATE style_templates SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(error)[:500], utc_now(), template_id),
            )
        raise


def start_analysis(template_id: str) -> None:
    thread = threading.Thread(target=_run_safely, args=(template_id,), daemon=True)
    thread.start()


def _run_safely(template_id: str) -> None:
    try:
        analyze(template_id)
    except Exception:  # noqa: BLE001 - 已写进库
        pass


# ------------------------------------------------------------------ 套用

def _facts_for(task_row) -> str:
    with transaction() as connection:
        row = connection.execute(
            "SELECT * FROM properties WHERE id=?", (task_row["property_id"],)
        ).fetchone()
    if row is None:
        raise StyleTemplateError("任务绑定的房源已不存在")
    public = public_property(row)
    fields = [
        ("区域", public["region"]),
        ("楼栋", public["building"]),
        ("房号", public["room"]),
        ("面积", public["area"]),
        ("格局", public["partition"]),
        ("朝向", public["orientation"]),
        ("家具", public["furnishing_layout"]),
        ("租金", public["rent"]),
    ]
    lines = [f"{label}：{value}" for label, value in fields if value]
    return "\n".join(lines) or "（这套房源没有可用字段）"


def apply_to_task(task_id: str, template_id: str) -> dict:
    with transaction() as connection:
        template = connection.execute(
            "SELECT * FROM style_templates WHERE id=?", (template_id,)
        ).fetchone()
    if template is None:
        raise StyleTemplateError("模板不存在")
    if template["status"] != "ready":
        raise StyleTemplateError("模板还没拆解完成")

    task = get_task(task_id)
    if task is None:
        raise StyleTemplateError("任务不存在")

    spec = json.loads(template["spec_json"] or "{}")
    shots = spec.get("shots") or []
    if not shots:
        raise StyleTemplateError("模板里没有可用的分镜结构")

    limit = min(int((spec.get("constraints") or {}).get("max_words_per_shot") or MAX_SHOT_TEXT), MAX_SHOT_TEXT)
    prompt = APPLY_PROMPT.format(
        spec=json.dumps(spec, ensure_ascii=False),
        facts=_facts_for(task),
        count=len(shots),
        limit=limit,
    )
    config = style_config()
    result = _chat([{"role": "user", "content": prompt}], config["text_model"])

    generated = result.get("shots") or []
    board = []
    for position, item in enumerate(generated[: len(shots)], start=1):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        role = str(item.get("role") or shots[position - 1].get("role") or "").strip()
        board.append({"index": position, "label": role[:40], "text": text})

    if not board:
        raise StyleTemplateError("模型没有生成可用文案")

    # 套模板前必须先有分镜 manifest（save_storyboard 会往里写）。
    # 任务若还没跑过 analyze，这里替用户跑一次，省得点了没反应。
    try:
        load_manifest(task["task_no"])
    except ShotError:
        if not task["source_video_path"]:
            raise StyleTemplateError("任务没有原始视频，无法生成分镜")
        try:
            analyze_shots(task["source_video_path"], task["task_no"])
        except ShotError as error:
            raise StyleTemplateError(f"分镜分析失败：{error}") from error

    try:
        return save_storyboard(task_id, board)
    except ShotError as error:
        raise StyleTemplateError(str(error)) from error


# ------------------------------------------------------------------ 链接下载

def download_video(url: str, target: Path) -> Path:
    """抖音/小红书链接 → 本地文件。用 yt-dlp，失败给明确提示。"""
    try:
        from yt_dlp import YoutubeDL
    except ImportError as error:
        raise StyleTemplateError("未安装 yt-dlp，无法下载链接；请手动下载视频后上传") from error

    target.parent.mkdir(parents=True, exist_ok=True)
    options = {
        "outtmpl": str(target.with_suffix(".%(ext)s")),
        "quiet": True,
        "no_warnings": True,
        "format": "mp4/best",
        "noplaylist": True,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except Exception as error:  # noqa: BLE001 - 各家异常类型不统一
        raise StyleTemplateError(
            f"链接下载失败：{str(error)[:200]}｜抖音有时要登录态，小红书建议手动下载后上传"
        ) from error

    if not path.is_file():
        raise StyleTemplateError("下载完成但找不到文件，请改为手动上传")
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise StyleTemplateError("下载的视频超过 512MB，请压缩后再上传")
    return path
