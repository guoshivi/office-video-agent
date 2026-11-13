import json
import random
import re
import string
import uuid
from datetime import datetime

from .db import EXPORT_DIR, get_settings, json_dumps, transaction, utc_now
from .hygiene import clean_internal_sentences

TASK_SELECT = """t.*, p.region_norm, p.building_norm, p.room_norm,
    p.area_raw, p.area_sqm, p.rent_raw, p.partition_raw, p.orientation_raw,
    p.furnishing_layout_raw, p.status AS property_status"""

SENSITIVE_PATTERN = re.compile(r"密码|门禁|钥匙|佣金|中介费|底价|红包|电话|手机|微信|议价", re.I)


def safe_public_text(value: str, limit: int = 60) -> str:
    value = re.sub(r"\s+", " ", (value or "")).strip()
    if not value or SENSITIVE_PATTERN.search(value):
        return ""
    return value[:limit]


def public_property(row) -> dict:
    return {
        "id": row["id"],
        "match_key": row["match_key"],
        "region": row["region_norm"],
        "building": row["building_norm"],
        "room": row["room_norm"],
        "area": row["area_raw"],
        "area_sqm": row["area_sqm"],
        "partition": row["partition_raw"],
        "rent": row["rent_raw"],
        "orientation": row["orientation_raw"],
        "furnishing_layout": row["furnishing_layout_raw"],
        "publish_date": row["publish_date_raw"],
        "source_sheet": row["source_sheet"],
        "source_row": row["source_row"],
        "is_new_marked": bool(row["is_new_marked"]),
        "is_special_marked": bool(row["is_special_marked"]),
        "status": row["status"],
        "updated_at": row["updated_at"],
    }


def _spoken_partition(raw: str) -> str:
    """把表里的「3隔」「5隔间」说成人话。"""
    text = (raw or "").strip()
    match = re.fullmatch(r"(\d+)\s*隔(间)?", text)
    if match:
        return f"{match.group(1)}个隔间"
    return text


def _spoken_orientation(raw: str) -> str:
    # 表里有的写「南」，有的写「朝南」，统一成「朝向 X」
    text = re.sub(r"^朝", "", (raw or "").strip())
    return text


def _cn_number(value: int) -> str:
    """11 → 十一，22 → 二十二（口播念数字比念阿拉伯数字自然）。"""
    digits = "零一二三四五六七八九"
    if value < 0:
        return str(value)
    if value < 10:
        return digits[value]
    if value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    if value < 100:
        tens, ones = divmod(value, 10)
        return digits[tens] + "十" + (digits[ones] if ones else "")
    return str(value)


def _partition_count(raw: str) -> int:
    """「3隔」「5隔间」「三个房间」都数成整数，数不出来算 0。

    表里这一列经常被填成面积（见过「2600平」），直接念会变成
    「隔了2600个房间」，所以带面积单位的一律当脏数据丢掉。
    """
    text = (raw or "").strip()
    if not text or re.search(r"平|㎡|m²|m2", text):
        return 0
    match = re.search(r"(\d+)", text)
    if match:
        count = int(match.group(1))
    else:
        match = re.search(r"[两二三四五六七八九]", text)
        if not match:
            return 0
        count = 2 if match.group(0) == "两" else "零一二三四五六七八九".index(match.group(0))
    return count if 0 < count <= 30 else 0


def _workstation_span(sqm: float) -> tuple[int, int]:
    """按行业经验把面积折成工位数：宽裕 10.5 平/位，紧凑 7.5 平/位。"""
    tight = max(1, int(round(sqm / 7.5)))
    loose = max(1, int(round(sqm / 10.5)))
    return min(loose, tight), max(loose, tight)


def _desk_phrase(sqm: float) -> str:
    """工位数说成人话：小数报区间，大数只报量级。

    量级必须按 high 落在哪一档来报，不能一句"≤300 就说两百多个"糊过去：
    800 平按 7.5~10.5 平/位是 76~107 个，照旧逻辑会报成"两百多个"，
    报多一倍，客户一进场就露馅。
    """
    low, high = _workstation_span(sqm)
    if high <= 30:
        return f"{_cn_number(low)}个到{_cn_number(high)}个之间"
    if high <= 100:
        mid = int(round((low + high) / 2 / 5) * 5)
        return f"{_cn_number(mid)}个上下"
    if high <= 130:
        return "近百个"
    if high <= 200:
        return "一百多个"
    if high <= 350:
        return "两百多个"
    return "三百个以上"


# 每句 10-20 字：出片时一句配一个镜头、一句一条字幕，太长字幕塞不下、
# 太短镜头切得人晕。句号收尾，渲染器按句号切分镜。
def _fit_audience(sqm: float) -> list[str]:
    if sqm >= 600:
        return ["这个面积一般是整层用，总部或者几个部门一起搬进来都放得下。"]
    if sqm >= 300:
        return ["三百多平往上，部门分区不用挤，老板间也能单独留出来。"]
    if sqm >= 150:
        return ["二十到三十人的公司正合适，前台、工区、会议室都分得出来。"]
    if sqm >= 80:
        return ["十几个人的团队，坐得开，又不至于空得慌。"]
    if sqm > 0:
        return ["小团队先把摊子支起来够用，或者拿来做工作室。"]
    return []


def _partition_lines(count: int) -> list[str]:
    if count <= 0:
        return []
    if count == 1:
        return ["一个大开间，中间没切，怎么分你自己定。"]
    if count == 2:
        return ["隔了两个房间，老板间和会议室各占一间，刚好。"]
    if count <= 4:
        return [f"隔了{_cn_number(count)}个房间，老板一间、财务一间、开会一间，正好分完。"]
    return [f"隔了{_cn_number(count)}个房间，团队怎么分都能落下。"]


def _orientation_lines(text: str) -> list[str]:
    if not text:
        return []
    if "四面" in text or "多面" in text:
        return ["四面采光，白天基本不用开灯，这是一套房里最值钱的地方。"]
    if "双面" in text:
        return ["双面采光，风从两边过，屋里不闷。"]
    if "南" in text:
        return ["朝南，上午十点往后光就进来了，下午两三点最亮。"]
    if "北" in text:
        return ["朝北，光线柔和不刺眼，盯一天屏幕也不累。"]
    if "东" in text:
        return ["朝东，早上光最好，一天从亮堂开始。"]
    if "西" in text:
        return ["朝西，下午采光足，加班到晚上也不闷。"]
    if "边" in text:
        return ["边套，采光面比中间户多，白天基本不用开灯。"]
    if "整层" in text:
        return ["整层独立，电梯门一开就是自己家，私密性好。"]
    return []


def _furnishing_lines(text: str) -> list[str]:
    """只认明确的装修/家具说法，读不懂的（比如被填成「产权面积」）宁可不念。"""
    if not text:
        return []
    if re.search(r"带全套|全套家具|家具齐|家私齐|拎包", text):
        return ["全套家具留着，桌椅一摆就能开工，这一块不用再花钱。"]
    if re.search(r"带家具|家私|有家具|带办公", text):
        return ["家具都留着，桌椅一摆就能开工，这一块不用再花钱。"]
    if re.search(r"毛坯|清水|空房", text):
        return ["现在是毛坯，装修你自己做主，想怎么改都行。"]
    if re.search(r"精装", text):
        return ["精装交付，不用再折腾装修，进来就能用。"]
    if re.search(r"阳台", text):
        return ["还带个阳台，想透口气不用下楼。"]
    if re.search(r"展厅", text):
        return ["之前是做展厅用的，开间大，改回办公也好改。"]
    return []


def _area_line(sqm: float, area_text: str) -> str:
    """面积句按量级换说法，避免每套房子都用同一句话开场。"""
    size = area_text or f"{_cn_number(int(round(sqm)))}平"
    if sqm >= 600:
        return f"{size}，整层量级，得按部门来规划。"
    if sqm >= 300:
        return f"{size}，这个面积能把分区做完整了。"
    if sqm >= 100:
        return f"{size}，标准办公面积，工位和房间都摆得开。"
    return f"{size}，小面积，实用为主，一寸都不浪费。"


def _judgment_lines(sqm: float) -> list[str]:
    """一句主观判断——中介带看时最常说的那种话，不涉及任何未证事实。"""
    if sqm >= 600:
        return ["说实话，整层这种盘不常有，碰到合适的别拖。"]
    if sqm >= 300:
        return ["这个面积段看房的老板最多，看着大、用着别扭的不少，这套算规矩的。"]
    if sqm >= 150:
        return ["二三十人的公司挑房子最难受，这套的尺寸刚好卡在舒服的位置。"]
    if sqm >= 80:
        return ["这个面积段最能看出性价比，差一点点，用起来差很多。"]
    if sqm > 0:
        return ["小面积好租也好转手，刚起步的公司拿这种最稳。"]
    return []


def generate_script(property_row, target_duration: int = 45) -> tuple[str, list[str]]:
    """生成能直接发出去的口播稿。

    这段文字会被视频工作台当文案，既做配音也做字幕，所以必须是**对外能看**的话：
    内部提醒只进 warnings，绝不写进 script；字段没把握的地方宁可不提，也不能编。

    写法律：房源字段能说什么就说什么；字段没写的，用「面积→工位」「隔间数→
    各自用途」这类行业经验换算补出体感（这是人会讲的话），但绝不断言现场细节
    （有没有柱子、什么地板、窗外是什么），那些交给「带你走一圈」。
    """
    settings = get_settings()
    region = safe_public_text(property_row["region_norm"], 30)
    building = safe_public_text(property_row["building_norm"], 30)
    room = safe_public_text(property_row["room_norm"], 20)
    area = safe_public_text(property_row["area_raw"], 20)
    partition = _spoken_partition(safe_public_text(property_row["partition_raw"], 20))
    orientation = _spoken_orientation(safe_public_text(property_row["orientation_raw"], 30))
    furnishing = safe_public_text(property_row["furnishing_layout_raw"], 60)

    raw_sqm = property_row["area_sqm"]
    sqm = float(raw_sqm) if raw_sqm else 0.0
    if not sqm and area:
        match = re.search(r"\d+(?:\.\d+)?", area)
        sqm = float(match.group(0)) if match else 0.0

    location = "、".join(part for part in (region, building) if part)
    # 同一套房源每次出片句式稳定（方便复用一条片子的话术），不同房源各不相同
    seed = "|".join((property_row["match_key"] or "", region, building, room))
    rng = random.Random(seed)

    blocks: list[tuple[str, list[str]]] = []
    # ---- 开场：有面积就用面积抛钩子，没有就说实话 ----
    if sqm:
        blocks.append(("hook", [rng.choice([
            f"{_cn_number(int(round(sqm)))}平的办公室，带你看一遍，看完再说值不值。",
            f"今天这套{_cn_number(int(round(sqm)))}平，先别急着划走，看完再说。",
            f"找房子的老板，{_cn_number(int(round(sqm)))}平这一套，值得你看完。",
        ])]))
    else:
        blocks.append(("hook", [rng.choice([
            "今天带你看一套新上的办公室，看完再决定。",
            "正在找办公室的老板，这一套先看完再划走。",
        ])]))

    # ---- 位置 ----
    where = "".join(part for part in (region, building) if part)
    if where and room:
        blocks.append(("location", [f"位置在{where}，{room}。"]))
    elif where:
        blocks.append(("location", [f"位置在{where}，具体几楼我发你。"]))
    elif room:
        blocks.append(("location", [f"房号{room}，位置我带你看。"]))

    # ---- 面积 + 工位体感 ----
    if sqm:
        blocks.append(("area", [_area_line(sqm, area)]))
        blocks.append(("desk", [f"工位摆下来，{_desk_phrase(sqm)}，看你留多少过道。"]))
        blocks.append(("desk2", [rng.choice([
            "这个尺寸，团队扩到明年也不用急着搬。",
            "工位之间留得开，坐着不憋。",
            "摆完工位还剩得出走道，不挤。",
        ])]))

    # ---- 格局 / 朝向 / 家具 ----
    count = _partition_count(partition)
    if count:
        blocks.append(("partition", _partition_lines(count)))
    if orientation:
        blocks.append(("orientation", _orientation_lines(orientation)))
    if furnishing:
        blocks.append(("furnishing", _furnishing_lines(furnishing)))

    # ---- 适合谁 + 一句主观判断（人味主要来自这里）----
    blocks.append(("fit", _fit_audience(sqm)))
    blocks.append(("judgment", _judgment_lines(sqm)))

    # ---- 带看邀约：现场细节不编，交给「走一圈」----
    blocks.append(("look", [rng.choice([
        "视频里只能看个大概，现场走一圈感受完全不一样。",
        "等下来了我从门口带你走一圈，动线你一看就明白。",
        "光看画面体会不到空间感，实地站一下最准。",
    ])]))

    # ---- 收尾 + 互动 ----
    contact = (settings.get("public_contact") or "欢迎私信咨询房源详情和看房安排").strip()
    blocks.append(("cta", [f"{contact}。"]))
    blocks.append(("engage", [rng.choice([
        "想看的老板评论区扣个 1，我把详细资料发你。",
        "有意的直接私信我，随时可以约看。",
        "拿不准的也可以问我，我按你的需求帮你挑。",
    ])]))

    # ---- 按目标时长裁剪：中文口播约每秒 4.2 字 ----
    budget = max(70, int(max(10, target_duration) * 4.2))
    lines: list[str] = []
    used = 0
    for name, candidates in blocks:
        for sentence in candidates:
            if not sentence:
                continue
            # 开场、收尾、互动必须留着——没有这三句就不像人说的话了
            mandatory = name in {"hook", "cta", "engage"}
            if not mandatory and used + len(sentence) > budget:
                continue
            lines.append(sentence)
            used += len(sentence)

    if not lines:
        lines = ["正在找办公室的朋友看过来，今天带你看一套新上的房源。"]

    script = "".join(lines)

    warnings = ["这是基于房源字段生成的口播稿，未经视频画面核验，出片前请过一遍。"]
    # 家具单独说，不并进下面那句"通用说法"：它不是缺字段那么简单，
    # 而是决定了文案能不能提家具（办公室租赁一般不带家具）。
    if not orientation:
        warnings.append("房源表里朝向是空的，文案已用通用说法兜住；补上这一列，稿子会更具体。")
    if not furnishing:
        # 办公室租赁一般不带家具，画面里的桌椅多半是上一个租客还没搬走
        warnings.append(
            "家具这一栏空着，文案按「不带家具」写：看到画面里的桌椅也不会说成配齐；"
            "确实带家具就把这一栏填上（写「带家具」），文案才会讲这一条。"
        )

    if sqm:
        warnings.append("工位数是按行业经验（约 7.5–10.5 平/工位）估算的，跟实际摆法会有出入。")
    if property_row["rent_raw"]:
        warnings.append("草稿没有播报租金。要在视频里报价，先把租金口径确认清楚再手动加。")
    if property_row["status"] == "needs_review":
        warnings.append("该房源存在重复或缺失匹配字段，出片前建议先确认房源本身。")
    return script, warnings


def new_task_no() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"V{datetime.now():%Y%m%d}-{suffix}"


# 任务级观感参数的合法取值。空字符串统一表示「跟系统设置」，所以不在这些集合里。
RENDER_OPTION_VALUES = {
    "subtitle_size": {"small", "medium", "large"},
    "subtitle_bottom": {"high", "middle", "low"},
}


def _pick(value: str, allowed: set[str]) -> str:
    """收任务级覆盖值：不认识的直接当没填，交给设置兜底，不把脏值写进库。"""
    text = (value or "").strip()
    return text if text in allowed else ""


def create_task_record(
    *,
    property_row,
    video_path,
    original_filename: str,
    target_duration: int = 45,
    aspect_ratio: str = "9:16",
    voice: str = "female_warm",
    style: str = "professional",
    source: str = "webui",
    notify_chat_id: str = "",
    task_no: str = "",
    script_override: str = "",
    audio_mode: str = "",
    subtitle_size: str = "",
    subtitle_bottom: str = "",
    tts_rate: str = "",
    flower: str = "",
) -> str:
    """WebUI 与飞书共用的唯一建任务入口，避免两个入口各写一套状态机。

    script_override 不为空时表示用户自己写了文案（或让 AI 写好再改过），
    直接用它，不再按字段重新生成。
    audio_mode / subtitle_size / subtitle_bottom / tts_rate / flower 空表示
    跟系统设置，填了就是这条任务自己的选择。
    """
    custom = (script_override or "").strip()
    if custom:
        script = custom if custom.endswith(("。", "！", "？", ".", "!", "?")) else custom + "。"
        warnings = ["用了你自己提供的文案，系统没有按房源字段核对，出片前请确认没有写错的信息。"]
    else:
        script, warnings = generate_script(property_row, target_duration)
    task_id = str(uuid.uuid4())
    now = utc_now()
    with transaction() as connection:
        connection.execute(
            """INSERT INTO tasks(
                id, task_no, property_id, source, original_filename, source_video_path,
                target_duration, aspect_ratio, voice, style, script, warnings_json,
                status, notify_chat_id, audio_mode, subtitle_size, subtitle_bottom,
                tts_rate, flower, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'script_ready', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, task_no or new_task_no(), property_row["id"], source,
                original_filename, str(video_path), target_duration, aspect_ratio,
                voice, style, script, json_dumps(warnings), notify_chat_id,
                (audio_mode or "").strip().lower(),
                _pick(subtitle_size, RENDER_OPTION_VALUES["subtitle_size"]),
                _pick(subtitle_bottom, RENDER_OPTION_VALUES["subtitle_bottom"]),
                (tts_rate or "").strip(),
                _pick(flower, {"0", "1"}),
                now, now,
            ),
        )
    return task_id


def set_task_script(task_id: str, script: str) -> bool:
    script = script.strip()
    if not script:
        return False
    with transaction() as connection:
        cursor = connection.execute(
            """UPDATE tasks SET script=?, status='script_ready',
               workbench_package_path='', updated_at=? WHERE id=?""",
            (script, utc_now(), task_id),
        )
        return cursor.rowcount > 0


def set_task_render_options(
    task_id: str,
    *,
    subtitle_size: str = "",
    subtitle_bottom: str = "",
    tts_rate: str = "",
    flower: str = "",
) -> bool:
    """改这条任务的观感覆盖项。传空字符串就是退回「跟设置」。

    只动这四列：改名目的时候不影响其它字段，也不触发重出片。
    """
    with transaction() as connection:
        cursor = connection.execute(
            """UPDATE tasks SET subtitle_size=?, subtitle_bottom=?, tts_rate=?,
               flower=?, updated_at=? WHERE id=?""",
            (
                _pick(subtitle_size, RENDER_OPTION_VALUES["subtitle_size"]),
                _pick(subtitle_bottom, RENDER_OPTION_VALUES["subtitle_bottom"]),
                (tts_rate or "").strip(),
                _pick(flower, {"0", "1"}),
                utc_now(),
                task_id,
            ),
        )
        return cursor.rowcount > 0


def find_task_by_no(task_no: str):
    with transaction() as connection:
        return connection.execute(
            f"""SELECT {TASK_SELECT} FROM tasks t JOIN properties p ON p.id=t.property_id
               WHERE UPPER(t.task_no)=UPPER(?)""",
            (task_no.strip(),),
        ).fetchone()


STATUS_LABELS = {
    "received": "已接收",
    "script_ready": "口播待确认",
    "exported": "已导出任务包",
    "processing": "出片中",
    "review": "待审片",
    "completed": "已完成",
    "failed": "失败",
}


def _property_line(row) -> str:
    parts = [row["region_norm"], row["building_norm"], row["room_norm"]]
    label = " ".join(part for part in parts if part) or "未命名房源"
    extras = [
        value for value in (
            f"{row['area_raw']}" if row["area_raw"] else "",
            row["partition_raw"],
            f"朝{row['orientation_raw']}" if row["orientation_raw"] else "",
            row["furnishing_layout_raw"],
        ) if value
    ]
    return f"{label}\n{'｜'.join(extras)}" if extras else label


def format_task_message(task, settings: dict, brief: bool = False) -> str:
    """给飞书用的纯文本卡片，尽量一行一个信息，手机上好读。"""
    label = STATUS_LABELS.get(task["status"], task["status"])
    progress = int(task["progress"] or 0)
    header = f"{task['task_no']} · {label}"
    lines = [header, _property_line(task)]

    if brief:
        if task["stage"]:
            lines.append(f"进度：{progress}% {task['stage']}")
        if task["error_message"]:
            lines.append(f"原因：{task['error_message']}")
        if task["output_video_path"]:
            lines.append(f"成片：{task['output_video_path']}")
        lines.append(f"口播：{task['script']}")
        return "\n".join(lines)

    lines.append("")
    lines.append("口播草稿：")
    lines.append(f"「{task['script']}」")
    warnings = json.loads(task["warnings_json"] or "[]")
    if warnings:
        lines.append("")
        lines.append("注意：" + warnings[0])
    lines.append("")
    lines.append(f"回复「出片 {task['task_no']}」开始生成")
    lines.append(f"回复「改口播 {task['task_no']} 新文案」修改口播")
    lines.append(f"回复「状态 {task['task_no']}」查看进度")
    return "\n".join(lines)


def get_task(task_id: str):
    with transaction() as connection:
        row = connection.execute(
            f"""SELECT {TASK_SELECT} FROM tasks t JOIN properties p ON p.id=t.property_id
               WHERE t.id=?""",
            (task_id,),
        ).fetchone()
    return row


def serialize_task(row, include_script: bool = True) -> dict:
    result = {
        "id": row["id"],
        "task_no": row["task_no"],
        "property_id": row["property_id"],
        "property_label": " ".join(
            part for part in (row["region_norm"], row["building_norm"], row["room_norm"]) if part
        ),
        "area": row["area_raw"],
        "rent": row["rent_raw"],
        "property_status": row["property_status"],
        "source": row["source"],
        "original_filename": row["original_filename"],
        "target_duration": row["target_duration"],
        "aspect_ratio": row["aspect_ratio"],
        "voice": row["voice"],
        "audio_mode": row["audio_mode"] if "audio_mode" in row.keys() else "",
        # 这四项空字符串表示「跟设置」。前端据此显示「跟设置」，不做成假的默认值。
        "subtitle_size": row["subtitle_size"] if "subtitle_size" in row.keys() else "",
        "subtitle_bottom": row["subtitle_bottom"] if "subtitle_bottom" in row.keys() else "",
        "tts_rate": row["tts_rate"] if "tts_rate" in row.keys() else "",
        "flower": row["flower"] if "flower" in row.keys() else "",
        "style": row["style"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "warnings": json.loads(row["warnings_json"] or "[]"),
        "workbench_package_path": row["workbench_package_path"],
        "output_video_path": row["output_video_path"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_script:
        result["script"] = row["script"]
    return result


def export_workbench_package(task_id: str) -> dict:
    task = get_task(task_id)
    if task is None:
        raise KeyError("任务不存在")
    with transaction() as connection:
        property_row = connection.execute(
            "SELECT * FROM properties WHERE id=?", (task["property_id"],)
        ).fetchone()
        settings = get_settings()

    package_dir = EXPORT_DIR / task["task_no"]
    package_dir.mkdir(parents=True, exist_ok=True)
    script_path = package_dir / "script.txt"
    manifest_path = package_dir / "task.json"
    # 导出的包会被别的出片工具直接拿去配音，所以内部提醒句在这里也摘一次
    export_script, removed = clean_internal_sentences(task["script"])
    script_path.write_text(export_script, encoding="utf-8")
    warnings = json.loads(task["warnings_json"] or "[]")
    if removed:
        warnings.append(f"导出时摘掉了 {len(removed)} 句内部用语：{'；'.join(removed)[:200]}")
    manifest = {
        "schema_version": 1,
        "task_id": task["id"],
        "task_no": task["task_no"],
        "created_at": utc_now(),
        "source_video": task["source_video_path"],
        "original_filename": task["original_filename"],
        "property": public_property(property_row),
        "render": {
            "target_duration": task["target_duration"],
            "aspect_ratio": task["aspect_ratio"],
            "voice": task["voice"],
            "style": task["style"],
        },
        "script_file": str(script_path),
        "warnings": warnings,
        "configured_workbench_path": settings.get("workbench_path", ""),
        "note": "This is an export package, not a rendered video. Connect the existing workbench adapter to process it.",
    }
    manifest_path.write_text(json_dumps(manifest), encoding="utf-8")
    with transaction() as connection:
        connection.execute(
            "UPDATE tasks SET status='exported', workbench_package_path=?, updated_at=? WHERE id=?",
            (str(package_dir), utc_now(), task_id),
        )
    return {"package_path": str(package_dir), "manifest_path": str(manifest_path)}
