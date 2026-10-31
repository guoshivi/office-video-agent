"""出片后的自动预检。

人工审片最怕的是"从头看一遍"，最有效的做法是"只看标红的"。所以这里把
能机器判断的都判断完，人只裁决剩下的：

- 事实：口播里说的面积/租金/隔间，跟房源库到底一不一致（最危险，写错就是带看纠纷）
- 串房：文案里出现了别的楼栋名，或者楼栋/房号对不上（拍错房、套错信息）
- 合规：引流词、绝对化用语、联系方式
- 成片：有没有音轨、是不是竖版、时长是否离谱
- 重复：同一个房源最近是不是刚发过

分级只有三档：blocking 必须改、warn 建议改、ok 不用管。
不追求"全自动通过"，追求"人一眼看到该看的地方"。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..db import get_settings, transaction
from ..hygiene import INTERNAL_PHRASES
from ..publish import policy
from ..shots import ShotError, _find_binary, _run

LEVEL_ORDER = {"ok": 0, "warn": 1, "blocking": 2}

# 面积说法多种多样：120平 / 120㎡ / 一百二十平 / 120平方米
_AREA_ARABIC = re.compile(r"(\d+(?:\.\d+)?)\s*(?:平方米|平方|平米|平|㎡|m²|m2)")
_AREA_CHINESE = re.compile(r"([零一二两三四五六七八九十百千万]{1,8})\s*(?:平方米|平方|平米|平)")
# 租金：12000元 / 1.2万 / 一万二
_RENT_ARABIC = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|万|元|块)")
_RENT_CHINESE = re.compile(r"([零一二两三四五六七八九十百千万]{1,8})\s*(万元|万|元|块)")
# 「一万二」「三万五」这种省略末位单位的说法：末尾那个字是千位
_RENT_COLLOQUIAL = re.compile(
    r"([一二两三四五六七八九])\s*万\s*([一二两三四五六七八九])(?![个间平块千元])"
)
# 隔间：3隔间 / 3+1 / 三个隔间
_PARTITION = re.compile(r"(\d+)\s*(?:\+1)?\s*(?:个)?(?:隔间|间)")

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

# 面积允许一点口语误差（"差不多一百二十平"），超过这个差就认为是说错了
AREA_TOLERANCE = 8.0
MIN_DURATION = 8
MAX_DURATION = 120

# 这些词属于"内部对内部"的表述，一旦进到口播里就是事故。
# 「口径」是最典型的：中介内部对数据口径有争议，但不能对客户说。
# 词表在 app/hygiene.py——出片前那次"摘句"用的是同一份，
# 否则会出现「生成时没拦、审核时才拦」的两份定义慢慢漂移。


@dataclass
class Check:
    id: str
    label: str
    level: str
    detail: str

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "level": self.level, "detail": self.detail}


@dataclass
class PrecheckReport:
    task_id: str
    task_no: str
    checks: list[Check] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)

    @property
    def level(self) -> str:
        return max((c.level for c in self.checks), key=lambda v: LEVEL_ORDER[v], default="ok")

    def by_level(self, level: str) -> list[Check]:
        return [c for c in self.checks if c.level == level]

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_no": self.task_no,
            "level": self.level,
            "summary": self.summary(),
            "extracted": self.extracted,
            "checks": [c.as_dict() for c in self.checks],
        }

    def summary(self) -> str:
        blocking = len(self.by_level("blocking"))
        warn = len(self.by_level("warn"))
        if blocking:
            return f"{blocking} 项必须处理" + (f"，{warn} 项建议看看" if warn else "")
        if warn:
            return f"{warn} 项建议看看"
        return "没发现问题"


# ------------------------------------------------------------------ 中文数字

def cn_to_int(text: str) -> int | None:
    """把「一百二十」「一万二」「三百五」这类中文数字转成整数。

    口语里的省略说法（一万二 = 12000、三百五 = 350）必须处理，
    否则「月租一万二」会被解析成 10002，跟房源库的 12000 永远对不上，
    预检就会一直误报。

    遇到不认识的写法返回 None，让上层退化成「无法判断」，
    而不是猜一个数出来误导审核。
    """
    text = (text or "").strip()
    if not text:
        return None
    total = 0
    section = 0
    number = 0
    last_unit = 0
    saw_zero = False
    for char in text:
        if char in _CN_DIGITS:
            if char == "零":
                saw_zero = True
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            last_unit = unit
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                # 「一百二」里的二表示二十，靠前有单位时补位
                section += (number or 1) * unit
            number = 0
        else:
            return None
    # 「一万二」「三百五」这类省略末位单位的说法，补的是上一级的十分之一
    if number and last_unit >= 100 and not saw_zero:
        total += number * (last_unit // 10)
        number = 0
    return total + section + number


# ------------------------------------------------------------------ 抽数

def extract_areas(text: str) -> list[float]:
    values: list[float] = []
    for raw in _AREA_ARABIC.findall(text or ""):
        try:
            values.append(float(raw))
        except ValueError:
            continue
    for raw in _AREA_CHINESE.findall(text or ""):
        value = cn_to_int(raw)
        if value:
            values.append(float(value))
    return values


def extract_rents(text: str) -> list[tuple[float, str]]:
    """抽出租金数字，统一换成"元"。

    先抓「一万二」这种省略说法并占位，否则「一万二」会被标准模式
    截成「一万」解析成 10000，跟房源库的 12000 永远对不上。
    """
    text = text or ""
    found: list[tuple[float, str]] = []
    consumed: list[tuple[int, int]] = []

    for match in _RENT_COLLOQUIAL.finditer(text):
        head = cn_to_int(match.group(1)) or 0
        tail = cn_to_int(match.group(2)) or 0
        found.append((float(head * 10000 + tail * 1000), "元"))
        consumed.append(match.span())

    def overlaps(span: tuple[int, int]) -> bool:
        return any(not (span[1] <= start or span[0] >= end) for start, end in consumed)

    for pattern, chinese in ((_RENT_ARABIC, False), (_RENT_CHINESE, True)):
        for match in pattern.finditer(text):
            if overlaps(match.span()):
                continue
            raw, unit = match.group(1), match.group(2)
            if chinese:
                value = cn_to_int(raw)
                if not value:
                    continue
                number = float(value)
            else:
                try:
                    number = float(raw)
                except ValueError:
                    continue
            found.append((number * 10000 if "万" in unit else number, unit))
    return found


def extract_partitions(text: str) -> list[int]:
    return [int(raw) for raw in _PARTITION.findall(text or "")]


# ------------------------------------------------------------------ 媒体体检

def probe_streams(video: Path) -> dict:
    """用 ffprobe 拿时长/宽高/有无音轨。失败不抛，交给上层降级成 warn。"""
    command = [
        _find_binary("ffprobe"), "-v", "error",
        "-show_entries", "stream=codec_type,width,height:format=duration",
        "-of", "json", str(video),
    ]
    result = _run(command, timeout=60)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise ShotError(f"ffprobe 输出无法解析：{(result.stderr or '')[:200]}") from error

    streams = payload.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        duration = float(payload.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "duration": duration,
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "has_audio": has_audio,
    }


# ------------------------------------------------------------------ 主入口

def precheck(task) -> PrecheckReport:
    """对一条已出片的任务做预检。传入 services.TASK_SELECT 查出来的行。"""
    report = PrecheckReport(task_id=task["id"], task_no=task["task_no"])
    script = (task["script"] or "").strip()

    # 1. 文案
    if not script:
        report.checks.append(Check("script", "口播文案", "blocking", "文案是空的，没法发布"))
    else:
        report.checks.append(Check("script", "口播文案", "ok", f"{len(script)} 字"))

    # 2. 成片
    raw_path = (task["output_video_path"] or "").strip()
    video = Path(raw_path) if raw_path else None
    media: dict = {}
    if video is None or not video.is_file():
        report.checks.append(Check("video", "成片文件", "blocking", "找不到成片文件"))
    else:
        size_mb = video.stat().st_size / 1048576
        report.checks.append(Check("video", "成片文件", "ok", f"{size_mb:.1f} MB"))
        try:
            media = probe_streams(video)
            report.extracted["media"] = media
        except (ShotError, OSError) as error:
            report.checks.append(
                Check("media", "视频体检", "warn", f"读不出视频参数：{error}")
            )

    if media:
        width, height = media.get("width", 0), media.get("height", 0)
        duration = media.get("duration", 0)
        # 读不到画面参数时不能断言"没有音轨"——ffprobe 对异常文件会返回空 stream，
        # 把它当成缺音轨会天天误报，只能降级成人自己看一眼。
        if not width and not duration:
            report.checks.append(
                Check("media", "视频体检", "warn", "读不出画面参数，请人工确认成片能正常播放")
            )
        else:
            if not media.get("has_audio"):
                report.checks.append(
                    Check("audio", "音轨", "blocking", "成片没有声音，配音没进去")
                )
            else:
                report.checks.append(Check("audio", "音轨", "ok", "有配音"))
            if width and height:
                if height <= width:
                    report.checks.append(
                        Check(
                            "aspect", "画幅", "warn",
                            f"{width}×{height} 是横版，小红书/抖音竖版流量更好",
                        )
                    )
                else:
                    report.checks.append(
                        Check("aspect", "画幅", "ok", f"{width}×{height} 竖版")
                    )
            if duration and duration < MIN_DURATION:
                report.checks.append(
                    Check("duration", "时长", "warn", f"{duration:.1f}s 偏短，信息量可能不够")
                )
            elif duration and duration > MAX_DURATION:
                report.checks.append(
                    Check("duration", "时长", "warn", f"{duration:.1f}s 偏长，完播率会掉")
                )
            elif duration:
                report.checks.append(Check("duration", "时长", "ok", f"{duration:.1f}s"))

    # 3. 面积事实
    _check_area(report, task, script)
    # 4. 租金事实
    _check_rent(report, task, script)
    # 5. 串房：楼栋/房号对不上
    _check_identity(report, task, script)
    # 6. 合规
    _check_compliance(report, task, script)
    # 7. 内部用语是否漏进了对外文案
    _check_hygiene(report, script)
    # 8. 重复发布
    _check_duplicate(report, task)

    return report


def _check_area(report: PrecheckReport, task, script: str) -> None:
    areas = extract_areas(script)
    report.extracted["areas"] = areas
    if not areas:
        report.checks.append(Check("area", "面积一致性", "ok", "文案没提面积"))
        return
    truth = task["area_sqm"]
    if not truth:
        report.checks.append(
            Check("area", "面积一致性", "warn", f"文案写了 {areas[0]:.0f} 平，但房源库没有面积数据")
        )
        return
    bad = [value for value in areas if abs(value - float(truth)) > AREA_TOLERANCE]
    if bad:
        report.checks.append(
            Check(
                "area", "面积一致性", "blocking",
                f"文案说 {bad[0]:.0f} 平，房源库是 {float(truth):.0f} 平",
            )
        )
    else:
        report.checks.append(
            Check("area", "面积一致性", "ok", f"文案与房源库都是 {float(truth):.0f} 平")
        )


def _check_rent(report: PrecheckReport, task, script: str) -> None:
    rents = extract_rents(script)
    report.extracted["rents"] = [value for value, _ in rents]
    if not rents:
        report.checks.append(Check("rent", "租金一致性", "ok", "文案没提具体租金"))
        return
    raw = (task["rent_raw"] or "").replace(",", "").replace("，", "")
    if not raw:
        report.checks.append(
            Check("rent", "租金一致性", "warn", "文案提到了租金，但房源库没有租金字段")
        )
        return
    # 文案口语化程度高（"一万二"、"12k"），只做宽松核对：数字要能在房源库里找到
    matched = False
    for value, _unit in rents:
        for form in (f"{value:.0f}", f"{value / 10000:g}", f"{value / 1000:g}"):
            if form and form in raw:
                matched = True
                break
        if matched:
            break
    if matched:
        report.checks.append(Check("rent", "租金一致性", "ok", f"与房源库「{raw}」对得上"))
    else:
        report.checks.append(
            Check(
                "rent", "租金一致性", "warn",
                f"文案提到 {rents[0][0]:.0f} 元，房源库是「{raw}」，请确认",
            )
        )


def _check_identity(report: PrecheckReport, task, script: str) -> None:
    """防串房：文案里出现别的楼栋名，或者提到房号但不是这一套。"""
    own_building = (task["building_norm"] or "").strip()
    own_room = (task["room_norm"] or "").strip()

    with transaction() as connection:
        rows = connection.execute(
            "SELECT DISTINCT building_norm FROM properties "
            "WHERE building_norm != '' AND building_norm != ? LIMIT 2000",
            (own_building,),
        ).fetchall()
    others = [row["building_norm"] for row in rows]
    # 只认长度 >= 3 的楼栋名，否则"大厦"这种通用词会疯狂误报
    hit = [name for name in others if len(name) >= 3 and name in script]

    if hit:
        report.checks.append(
            Check(
                "identity", "房源一致性", "blocking",
                f"文案里出现了别的楼栋「{hit[0]}」，但这条任务绑的是「{own_building}」，疑似串房",
            )
        )
    elif own_building and own_building not in script:
        report.checks.append(
            Check(
                "identity", "房源一致性", "warn",
                f"文案没有提到楼栋「{own_building}」，观众不知道这是哪",
            )
        )
    elif own_room and own_room not in script:
        report.checks.append(
            Check("identity", "房源一致性", "ok", f"绑定的房号 {own_room} 未在文案中出现，属正常")
        )
    else:
        report.checks.append(Check("identity", "房源一致性", "ok", "楼栋与文案一致"))


def _check_compliance(report: PrecheckReport, task, script: str) -> None:
    issues = policy.scan_text(script)
    blocking = [item for item in issues if item.level == "blocking"]
    warns = [item for item in issues if item.level == "warn"]
    for item in blocking:
        report.checks.append(Check("compliance", "合规", "blocking", item.message))
    for item in warns:
        report.checks.append(Check("compliance", "合规", "warn", item.message))
    if not issues:
        report.checks.append(Check("compliance", "合规", "ok", "没发现引流词和绝对化用语"))


def _check_hygiene(report: PrecheckReport, script: str) -> None:
    """内部用语不能出现在对外文案里。

    这条是看真实成片时发现的：内部提醒「租金和物业费口径请以最新房源确认
    为准」被念进了口播。对客户说这种话，等于当面承认自己数据没底。
    机器能查的就不该靠人眼去听。
    """
    hits = [phrase for phrase in INTERNAL_PHRASES if phrase in script]
    if hits:
        report.checks.append(
            Check(
                "hygiene", "对外措辞", "blocking",
                "文案里有内部用语：" + "、".join(f"「{item}」" for item in hits[:3]),
            )
        )
    else:
        report.checks.append(Check("hygiene", "对外措辞", "ok", "没有内部提醒漏出去"))


def _check_duplicate(report: PrecheckReport, task) -> None:
    with transaction() as connection:
        rows = connection.execute(
            "SELECT r.platform, r.status, r.created_at FROM publish_records r "
            "JOIN tasks t ON t.id = r.task_id "
            "WHERE t.property_id = ? AND r.task_id != ? "
            "AND r.status IN ('published','uncertain') "
            "ORDER BY r.created_at DESC LIMIT 5",
            (task["property_id"], task["id"]),
        ).fetchall()
    if rows:
        platforms = "、".join(sorted({row["platform"] for row in rows}))
        report.checks.append(
            Check(
                "duplicate", "重复发布", "warn",
                f"这套房源已经发过 {len(rows)} 次（{platforms}），同质内容容易被判重复",
            )
        )
    else:
        report.checks.append(Check("duplicate", "重复发布", "ok", "这套房源还没发过"))


def precheck_and_save(task) -> dict:
    """跑预检并按任务存档。

    真正的存档动作在 service.run_precheck 里（那里才该知道 store），
    这里保留入口只是为了让命令行工具能一行调用。
    """
    from .service import run_precheck

    return run_precheck(task["id"])
