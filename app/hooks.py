"""钩子库：从 data/copy/hooks.json 读开头模板，按房源事实渲染成口播第一句。

为什么单独一层：文案里最值钱的不是卖点，是前 3 秒。这一层只负责开头，
跟「怎么把剩下的句子写完」解耦——钩子可以单独调、单独 A/B，改 JSON
不用动代码。

房源字段能推出来的东西也在这里算：
    月租 monthly   从「3.5元/平/天」或「2.8万/月」统一折成元
    日均 daily     月租 / 30，中介实际谈价时都这么算
    工位 seats     面积 / 8（含公摊的常见密度）
    人均 per_person 月租 / 工位数

算不出来的变量留空，用到它的模板自动跳过，不会渲染出「一天只要」这种
后半截没词的句子。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LIBRARY_PATH = Path(__file__).resolve().parent.parent / "data" / "copy" / "hooks.json"

# mtime 变了就重读，改 JSON 不用重启服务
_CACHE: dict[str, tuple[float, dict]] = {}


class HookError(RuntimeError):
    pass


# ------------------------------------------------------------------ 读库

def load(path: Path | str | None = None) -> dict:
    """读钩子库。文件不存在时返回空库，调用方自行兜底。"""
    target = Path(path) if path else LIBRARY_PATH
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return {"version": 0, "audiences": [], "tiers": []}

    key = str(target)
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("钩子库读取失败，本次跳过：%s", str(error)[:200])
        return {"version": 0, "audiences": [], "tiers": []}

    _CACHE[key] = (mtime, data)
    return data


def tiers(path: Path | str | None = None) -> list[dict]:
    """三档分类，给前端下拉和文案说明用。"""
    return [t for t in (load(path).get("tiers") or []) if t.get("hooks")]


def audiences(path: Path | str | None = None) -> list[dict]:
    return list(load(path).get("audiences") or [])


# ------------------------------------------------------------------ 房源变量

def _first_number(text: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(text or ""))
    return float(match.group(0)) if match else None


def _money(value: float) -> str:
    """28000 → 2.8万；3500 → 3500；550 → 550。"""
    if value >= 10000:
        text = f"{value / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{text}万"
    return f"{round(value)}"


def _daily_money(value: float) -> str:
    """日均通常几十到几百，整数最像人话；小额保留一位小数。"""
    if value >= 100:
        return f"{round(value)}"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def monthly_rent(facts: dict, area: float | None = None) -> float | None:
    """把各种写法的租金统一折算成「元/月」。

    能吃下这几种：3.5元/平/天、3.5/㎡/天、2.8万/月、28000元/月、
    28/平/月。判断不了单位的，按「单价」理解，面积也没有就返回 None。

    最后过一道合理性校验：折出来的单价必须在 8~800 元/平/月之间。
    房源表里 rent 字段填错的（比如把「35+5 格局」写进租金）会算出
    「一天五万六」这种鬼话，宁可这句成本话不说，也不能在视频里报价。
    """
    raw = str(
        facts.get("租金") or facts.get("rent") or facts.get("rent_raw") or ""
    ).strip()
    if not raw:
        return None
    base = _first_number(raw)
    if not base:
        return None

    is_wan = "万" in raw
    amount = base * 10000 if is_wan else base
    per_area = ("平" in raw or "㎡" in raw or "m2" in raw.lower())
    per_day = ("天" in raw or "日" in raw)

    value: float | None
    if per_area and per_day:
        value = amount * area * 30 if area else None
    elif per_area:
        value = amount * area if area else None
    elif per_day:
        value = amount * 30
    elif "月" in raw:
        value = amount
    elif amount >= 1000:
        value = amount
    else:
        value = amount * area * 30 if area else None

    if value and value > 0 and area:
        unit_price = value / area
        if not (8 <= unit_price <= 800):
            logger.info("租金折算不合理（%.0f 元/平/月），这句成本话不出：%s", unit_price, raw)
            return None
    return value


def context(facts: dict) -> dict[str, str]:
    """房源事实 → 模板变量。只放能算准的，算不准的直接不出现。"""
    area_number = _first_number(facts.get("面积") or facts.get("area") or "")
    ctx: dict[str, str] = {}

    if area_number:
        ctx["area"] = f"{area_number:g}"

    for key, name in (
        ("区域", "region"), ("region", "region"),
        ("楼栋", "building"), ("building", "building"),
        ("格局", "partition"), ("partition", "partition"),
        ("朝向", "orientation"), ("orientation", "orientation"),
        ("家具", "furnishing"), ("家具装修", "furnishing"), ("furnishing_layout", "furnishing"),
    ):
        value = str(facts.get(key) or "").strip()
        if value and name not in ctx:
            ctx[name] = value

    rent_text = str(facts.get("租金") or facts.get("rent") or "").strip()
    if rent_text:
        ctx["rent"] = rent_text

    monthly = monthly_rent(facts, area_number)
    if monthly and monthly > 0:
        ctx["monthly"] = _money(monthly)
        ctx["daily"] = _daily_money(monthly / 30)

        seats = max(2, math.ceil(area_number / 8)) if area_number else None
        if seats:
            ctx["seats"] = str(seats)
            ctx["per_person"] = _money(monthly / seats)

    return ctx


# ------------------------------------------------------------------ 选钩子

def _matches_audience(hook: dict, audience: str) -> bool:
    tags = [str(t) for t in (hook.get("aud") or [])]
    if audience in ("", "any", None):
        # 不限人群时只出泛用款，人群定向的话术容易让另一拨人觉得不关己
        return "any" in tags
    return "any" in tags or audience in tags


def _render(tpl: str, ctx: dict[str, str]) -> str | None:
    """变量齐了才出句子；少一个就整条作废，不留半截话。"""
    needed = re.findall(r"\{(\w+)\}", tpl)
    if any(not ctx.get(name) for name in needed):
        return None
    try:
        return tpl.format(**ctx)
    except (KeyError, IndexError):
        return None


def candidates(facts: dict, audience: str = "any", path: Path | str | None = None) -> list[dict]:
    """所有能直接用的钩子，按档次顺序排好。"""
    ctx = context(facts)
    out: list[dict] = []
    for tier in tiers(path):
        for hook in tier.get("hooks") or []:
            if not _matches_audience(hook, audience):
                continue
            text = _render(str(hook.get("tpl") or ""), ctx)
            if not text:
                continue
            out.append({
                "tier": tier.get("key", ""),
                "tier_label": tier.get("label", ""),
                "text": text,
            })
    return out


def _rotate(items: list, seed: str) -> list:
    """同一套房源每次拿到的顺序稳定，换房源才换顺序——不然同一条片子重跑
    开头会变，中介没法对比哪版好。"""
    if not items:
        return []
    digest = hashlib.sha1(str(seed).encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % len(items)
    return items[offset:] + items[:offset]


def pick(facts: dict, audience: str = "any", count: int = 3, seed: str = "") -> list[dict]:
    """挑 N 条互不重复的钩子，尽量一档一条，方便直接对比三版。"""
    pools: dict[str, list[dict]] = {}
    for item in candidates(facts, audience):
        pools.setdefault(item["tier"], []).append(item)
    if not pools:
        return []

    order = [t.get("key", "") for t in tiers() if t.get("key") in pools]
    # 每档先按 seed 转一下，换房源就换开头，同一套房源重跑保持一致
    base = seed or f"{audience}:{json.dumps(facts, ensure_ascii=False, sort_keys=True)}"
    pools = {key: _rotate(pool, f"{base}-{key}") for key, pool in pools.items()}

    chosen: list[dict] = []
    index = 0
    # 轮着从每档拿一条，保证三版风格不一样
    while len(chosen) < count and order:
        progressed = False
        for key in order:
            pool = pools.get(key) or []
            if index < len(pool):
                chosen.append(pool[index])
                progressed = True
                if len(chosen) >= count:
                    break
        if not progressed:
            break
        index += 1

    return chosen


def describe(path: Path | str | None = None) -> dict:
    """给 /api/health 和前端用的概览：几档、几条、更新日期。"""
    data = load(path)
    tier_list = data.get("tiers") or []
    return {
        "version": data.get("version", 0),
        "updated": data.get("updated", ""),
        "tiers": len(tier_list),
        "hooks": sum(len(t.get("hooks") or []) for t in tier_list),
        "audiences": len(data.get("audiences") or []),
    }
