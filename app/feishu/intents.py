"""飞书消息里的自然语言意图解析与房源定位。

中介在飞书上打字很随意：`A区 中关村大厦 1201`、`a区-中关村-1201`、
只回一个 `1201`，甚至夹一句“这个视频”。这里负责把这些收敛成确定的房源记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..db import transaction

# 全角转半角，统一分隔符与大小写，避免同一个房源被当成两条。
_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH[0x3000] = 0x20
# 注意这里不含 "-"：房源里 520/809 条房号本身带横杆（202-2、1102-3），
# 拆开就会变成「区/楼栋/数字」，指向完全不相干的房源。
_SEPARATORS = re.compile(r"[\s_—/\\、,，;；|·.。:：()（）\[\]【】\"'“”]+")
# 整串比对时把所有分隔符都抹掉，这样「A区-A01-101」「a区 a01 101」是同一个键。
_SQUASH = re.compile(r"[\s\-_—/\\、,，;；|·.。:：()（）\[\]【】\"'“”]+")

HELP_TEXT = """房源视频助手 · 可用指令

1. 直接发视频，再回复房源信息
   例：A区 中关村大厦 1201
   只会生成口播草稿，不会立刻出片

2. 出片 V20251101-ABCDE
   提交视频工作台渲染成片

3. 改口播 V20251101-ABCDE 新的口播文案
   覆盖口播草稿后需重新出片

4. 状态 V20251101-ABCDE
   查看单个任务进度

5. 房源 A区 100平 带家具
   按关键词或语义检索在租房源（最多 5 条）

其他：帮助 / 取消 —— 取消当前待处理视频"""


def normalize(value: str) -> str:
    return str(value or "").translate(_FULLWIDTH).strip()


def key_part(value: str) -> str:
    return re.sub(r"\s+", "", normalize(value)).upper()


def tokens_of(text: str) -> list[str]:
    cleaned = normalize(text)
    return [token for token in _SEPARATORS.split(cleaned) if token]


def squash(value: str) -> str:
    """抹掉所有分隔符并大写，用于整串房源键的等价比较。"""
    return _SQUASH.sub("", normalize(value)).upper()


@dataclass
class Intent:
    action: str
    text: str = ""
    task_no: str = ""
    payload: str = ""
    content: str = ""


COMMAND_PREFIXES = {
    "出片": "render",
    "渲染": "render",
    "生成": "render",
    "改口播": "script",
    "改文案": "script",
    "口播": "script",
    "状态": "status",
    "进度": "status",
    "查询": "status",
    "房源": "search",
    "找房": "search",
    "搜索": "search",
    "帮助": "help",
    "help": "help",
    "?": "help",
    "取消": "cancel",
    "reset": "cancel",
}

TASK_NO_PATTERN = re.compile(r"(V\d{8}-[A-Z0-9]{4,8})", re.I)


def parse_command(text: str) -> Intent | None:
    raw = normalize(text)
    if not raw:
        return None
    task_match = TASK_NO_PATTERN.search(raw)
    task_no = task_match.group(1).upper() if task_match else ""
    head, _, tail = raw.partition(" ")
    if not tail and task_no and head.upper() == task_no:
        return Intent(action="status", task_no=task_no, content=raw)

    keyword = head.strip().lower()
    action = COMMAND_PREFIXES.get(keyword)
    if action is None:
        # 也接受「出片=V2025...」这类没有空格的写法
        for prefix, mapped in COMMAND_PREFIXES.items():
            if raw.lower().startswith(prefix) and len(raw) > len(prefix):
                action = mapped
                tail = raw[len(prefix):].strip(" :：=")
                break
    if action is None:
        return None
    if action in {"render", "script", "status"}:
        payload = tail.strip()
        # 编号通常写在最前面，去掉它才是真正的文案
        if task_no and payload.upper().startswith(task_no):
            payload = payload[len(task_no):].strip(" :：=，,")
        return Intent(action=action, task_no=task_no, payload=payload, content=raw)
    if action == "search":
        return Intent(action="search", text=tail.strip(), content=raw)
    return Intent(action=action, content=raw)


@dataclass
class MatchResult:
    kind: str  # matched | ambiguous | none
    property: object | None = None
    candidates: list = field(default_factory=list)
    hint: str = ""


def resolve_property(text: str, limit: int = 5) -> MatchResult:
    tokens = tokens_of(text)
    if not tokens:
        return MatchResult("none", hint="没有识别到房源信息")

    # 去掉“视频/这个/的”这类口语噪声词，它们从来不是房源字段。
    noise = {"视频", "这个", "那个", "拍", "一下", "房源", "的", "吧", "呢", "是"}
    core = [token for token in tokens if token not in noise] or tokens

    # 第一层：整串键精确匹配。
    # 「F区 F14 202-2」「f区-f14-202-2」「F区F14202-2」都应该落到同一条，
    # 而且房号里的横杆不能被当成分隔符。
    rows = _query_by_full_key(squash("".join(core)))
    if len(rows) == 1:
        return MatchResult("matched", property=rows[0])
    if len(rows) > 1:
        return MatchResult("ambiguous", candidates=rows[:limit])

    # 第二层：猜 区/楼栋/房号 三段，只给两个词或一个词时逐步放宽。
    room = core[-1]
    building = core[-2] if len(core) >= 2 else ""
    region = core[-3] if len(core) >= 3 else ""

    rows = _query(region, building, room)
    if not rows and len(core) == 1:
        # 只给了一个词：可能是楼栋名，也可能是房号
        rows = _query("", "", core[0], fuzzy_all=True)
    confident = True
    if not rows and building:
        # 区域写错或省略，退一步只用楼栋+房号
        rows = _query("", building, room)
    if not rows and len(core) >= 2:
        # 最后只按房号兜底。命中可能只是「别的楼栋同号房」，
        # 所以即使唯一也不直接认定，交回人工确认，避免报错地址。
        rows = _query("", "", room)
        confident = False

    if len(rows) == 1:
        if confident:
            return MatchResult("matched", property=rows[0])
        return MatchResult(
            "ambiguous",
            candidates=rows,
            hint=f"只按房号匹配到一条：{describe_property(rows[0])}，请确认",
        )
    if len(rows) > 1:
        exact = [row for row in rows if row["match_key"] and
                 row["match_key"] == "|".join(key_part(p) for p in (region, building, room))]
        if len(exact) == 1:
            return MatchResult("matched", property=exact[0])
        return MatchResult("ambiguous", candidates=rows[:limit])
    return MatchResult("none", hint=f"没有找到「{text.strip()}」对应的房源")


def _query_by_full_key(key: str) -> list:
    """整串比对 match_key。表里只有几百行，逐行比 squash 值最不容易出错。"""
    if not key:
        return []
    with transaction() as connection:
        rows = connection.execute(
            """SELECT * FROM properties
               ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, source_sheet, source_row"""
        ).fetchall()
    return [row for row in rows if squash(row["match_key"]) == key]


def _query(region: str, building: str, room: str, fuzzy_all: bool = False) -> list:
    # 房号先按完全相等找，找不到再退回模糊：否则「102」会把「1102-2」也捞进来
    rows = _query_with(region, building, room, fuzzy_all, exact_room=True)
    if not rows:
        rows = _query_with(region, building, room, fuzzy_all, exact_room=False)
    return rows


def _query_with(region: str, building: str, room: str, fuzzy_all: bool, exact_room: bool) -> list:
    clauses, params = ["room_norm <> ''"], []
    room_value = normalize(room)
    if fuzzy_all:
        keyword = f"%{room_value}%"
        clauses.append("(room_norm LIKE ? OR building_norm LIKE ?)")
        params.extend([keyword, keyword])
    elif exact_room:
        clauses.append("room_norm = ?")
        params.append(room_value)
    else:
        clauses.append("room_norm LIKE ?")
        params.append(f"%{room_value}%")
    if building:
        clauses.append("building_norm LIKE ?")
        params.append(f"%{normalize(building)}%")
    if region:
        clauses.append("region_norm LIKE ?")
        params.append(f"%{normalize(region)}%")
    sql = (
        "SELECT * FROM properties WHERE "
        + " AND ".join(clauses)
        + " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, source_sheet, source_row LIMIT 20"
    )
    with transaction() as connection:
        return connection.execute(sql, params).fetchall()


def describe_property(row) -> str:
    parts = [row["region_norm"], row["building_norm"], row["room_norm"]]
    label = " ".join(part for part in parts if part) or "未命名房源"
    extras = []
    if row["area_raw"]:
        extras.append(f"{row['area_raw']}")
    if row["partition_raw"]:
        extras.append(row["partition_raw"])
    if row["orientation_raw"]:
        extras.append(f"朝{row['orientation_raw']}")
    if row["furnishing_layout_raw"]:
        extras.append(row["furnishing_layout_raw"])
    return f"{label} · " + "｜".join(extras) if extras else label
