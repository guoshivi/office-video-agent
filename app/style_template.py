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


def _engine() -> str:
    """拆解引擎。

    默认 local：ffmpeg 找切点 + OCR 读字幕，离线、免费、零依赖。
    方舟视频理解留成可选增强（STYLE_ENGINE=ark 且有 key），因为它要先把整条片子
    传上去做预处理，又慢又贵，而且它自己判镜头，和拼图复核用的那套时间轴对不上。
    """
    return (os.getenv("STYLE_ENGINE", "").strip() or "local").lower()


def _analyze_local(path: Path) -> dict:
    """本地拆：切点 / 字幕 / 等分占位，跟拼图复核用的是同一份 spec。"""
    from .refvideo import RefVideoError, analyze_file

    try:
        return analyze_file(path)
    except RefVideoError as error:
        raise StyleTemplateError(str(error)) from error


def _transcript_of(spec: dict) -> str:
    return "\n".join(
        str(item.get("text") or "") for item in (spec.get("sentences") or []) if item.get("text")
    )


def analyze(template_id: str) -> dict:
    """后台线程里跑：拆解 → 落库。

    拆解是本地能力，不该被云端 key 挡住：没有 ARK_API_KEY 也必须能拆出一条片子，
    否则「上传 → 看拼图 → 复核」这条主链路在最常见的环境里第一步就断了。
    """
    with transaction() as connection:
        row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise StyleTemplateError("模板不存在")

    try:
        path = Path(row["source_path"])
        if not path.is_file():
            raise StyleTemplateError("参考视频文件已丢失，请重新上传")

        duration = probe_duration(path)
        if _engine() == "local":
            spec = _analyze_local(path)
            transcript = _transcript_of(spec)
            with transaction() as connection:
                connection.execute(
                    """UPDATE style_templates SET status='ready', spec_json=?, transcript=?,
                       duration_s=?, error='', updated_at=? WHERE id=?""",
                    (json_dumps(spec), transcript, spec.get("duration") or duration, utc_now(), template_id),
                )
            logger.info("风格模板拆解完成（本地）：%s（%s）", template_id, _spec_summary(spec))
            return spec

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

def _property_for(task_row) -> dict:
    with transaction() as connection:
        row = connection.execute(
            "SELECT * FROM properties WHERE id=?", (task_row["property_id"],)
        ).fetchone()
    if row is None:
        raise StyleTemplateError("任务绑定的房源已不存在")
    return public_property(row)


def _facts_for(task_row) -> str:
    public = _property_for(task_row)
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


# 卖点镜轮着讲什么。一条片子连着四个"亮点"镜时，不能四句都说采光。
_SPOT_ROTATION = ("zone", "furn", "boss", "light")
# 换卖点只能换成画面里真有的东西。没有这张表，程序会因为"furn 说过了"就去讲
# "独立老板间"——可这一镜拍的是靠窗工位，客户一句"老板间在哪"就把人问住了。
_TOPIC_HINTS: dict[str, tuple[str, ...]] = {
    "zone": ("走廊", "分区", "动线", "隔断", "过道", "入口", "通道", "工区"),
    "furn": ("工位", "家具", "桌", "椅", "沙发", "柜", "茶几", "茶桌", "办公"),
    "boss": ("老板间", "独立间", "单间", "隔间", "经理室", "会议室", "独立办公"),
    "light": ("窗", "采光", "阳光", "光线", "通透", "朝南", "朝东"),
}


def _hits(subject: str, topic: str) -> bool:
    """这一镜的画面上，到底有没有某个卖点。"""
    text = str(subject or "")
    return bool(text) and any(word in text for word in _TOPIC_HINTS.get(topic, ()))

_ROLE_TO_TOPIC = {
    "hook": "hook",
    "area": "area", "面积": "area",
    "value": "value", "价格": "value",
    "cta": "cta",
    # 复核时人明确填了"这镜是采光/家具/分区"，就照填，别再拿轮换覆盖他的判断
    "zone": "zone", "分区": "zone", "工区": "zone",
    "furn": "furn", "家具": "furn",
    "boss": "boss", "老板间": "boss",
    "light": "light", "采光": "light",
    "亮点": None, "spot": None, "": None,
}


def _compact_spec(spec: dict) -> dict:
    """把模板压成「一镜一句」。

    这一步很关键：原始拆解是**逐句**的，一条 25 秒的爆款能拆出 17 句台词。
    照着 17 句写，开头会连着三句钩子、四个卖点镜各说一句，念出来又臭又长。

    模板真正值钱的是镜头结构——几个镜头、每镜担什么角色、什么顺序。
    所以套用时按镜头压成一句，镜头数才是文案句数。
    """
    scenes = spec.get("scenes") or []
    if not scenes:
        # 没复核过的模板没有镜头语义，退回按原 shots 的 role 去重
        compact, seen = [], set()
        for shot in spec.get("shots") or []:
            role = str(shot.get("role") or "")
            key = role or f"_{len(compact)}"
            if key in seen:
                continue
            seen.add(key)
            compact.append({"index": len(compact) + 1, "role": role, "sample": ""})
        return {**spec, "shots": compact}

    cursor = 0
    used: set[str] = set()
    spots = set(_SPOT_ROTATION)
    shots = []
    for item in scenes:
        role = str(item.get("role") or "")
        subject = str(item.get("subject") or "")
        topic = _ROLE_TO_TOPIC.get(role)
        # 同一个卖点第二次出现时想换个说法：连着三句"采光通透""整面落地窗""白天不用开灯"
        # 听着像卡带。但换的前提是画面里真有别的卖点——
        # 只换成 subject 里写得出来的，一个都对不上就照原卖点再说一遍，由句库换句子。
        # 宁可同一个卖点说两次，也不能凭空造一个画面上没有的空间。
        if topic in used and topic in spots and not _hits(subject, topic):
            spare = [t for t in _SPOT_ROTATION if t not in used and _hits(subject, t)]
            if spare:
                topic = spare[0]
        if topic is None:
            for offset in range(len(_SPOT_ROTATION)):
                candidate = _SPOT_ROTATION[(cursor + offset) % len(_SPOT_ROTATION)]
                if candidate not in used:
                    topic = candidate
                    cursor = (cursor + offset + 1) % len(_SPOT_ROTATION)
                    break
            else:
                topic = _SPOT_ROTATION[cursor % len(_SPOT_ROTATION)]
                cursor += 1
        used.add(topic)
        shots.append({
            "index": len(shots) + 1,
            "role": topic,
            "sample": "",
            "subject": item.get("subject", ""),
        })

    # 参考片里没有收尾镜时补一镜。空房实拍素材几乎都没有引导（拍完就走），
    # 但没有收尾的片子等于没告诉观众下一步干什么，套用后必须补上。
    # 注意别把这句塞进最后一镜：那是"长条工位桌"，配"感兴趣的老板过来看看"是错位。
    if shots and all(shot["role"] != "cta" for shot in shots):
        shots.append({
            "index": len(shots) + 1,
            "role": "cta",
            "sample": "",
            "subject": "补拍：收尾引导（参考片没有这一镜）",
            "added": True,
        })
    return {**spec, "shots": shots}


def _local_board(spec: dict, public: dict) -> list[dict]:
    """没有云端模型时，用本地句式库按模板结构套写。

    这条路不需要任何 API Key，也不联网：模板提供的是「几镜、每镜讲什么」，
    内容全部来自房源字段和已经验证过的句式库，不会编出面积和价格。
    """
    from .copywriting import CopywritingError, fallback_storyboard

    compact = _compact_spec(spec)
    try:
        board = fallback_storyboard(compact, public)
    except CopywritingError as error:
        raise StyleTemplateError(str(error)) from error

    roles = {shot["index"]: str(shot.get("role") or "") for shot in compact.get("shots") or []}
    clean: list[dict] = []
    for position, shot in enumerate(board, start=1):
        text = str(shot.get("text") or "").strip()
        if not text:
            continue
        clean.append({
            "index": position,
            "label": roles.get(position, "")[:40],
            "text": text,
        })
    if not clean:
        raise StyleTemplateError("这套房源的信息不够，本地引擎写不出文案；补上面积和租金再试")
    return clean


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

    # 套用前先压成「一镜一句」：原模板是逐句拆的，一条 25 秒的片子能出十几句，
    # 照那个句数写会变成连着三句钩子、四个卖点镜各说一句
    spec = _compact_spec(json.loads(template["spec_json"] or "{}"))
    shots = spec.get("shots") or []
    if not shots:
        raise StyleTemplateError("模板里没有可用的分镜结构")

    engine = "local"
    if style_config()["api_key"]:
        engine = "cloud"
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
    else:
        board = _local_board(spec, _property_for(task))

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
        result = save_storyboard(task_id, board)
    except ShotError as error:
        raise StyleTemplateError(str(error)) from error
    result["engine"] = engine
    return result


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
