"""按模板生成口播文案：有 ARK_API_KEY 走大模型，没有就用规则套写。

模板 spec 来自参考视频拆解（refvideo / style_template），里面有逐句的
角色（hook / 面积 / 亮点 / cta）、字数上限和口吻。套用到一套新房源时：

1. **API 路** —— 火山方舟 chat：把模板结构和房源事实给它，让它按
   模板的句数、角色、节奏写一遍，内容只准用给定事实。写完做去重校验
   （相邻句不能同义，总面积钩子不能凭空出现两次）。
2. **规则路** —— 没有 key 也能出片：按角色从句式库里挑模板套房源字段，
   同样的去重逻辑。观感比大模型生硬，但每句都有信息量、不会胡编。

两条路输出同一个 storyboard 结构（text + 可选 label），直接喂渲染器。
"""

from __future__ import annotations

import json
import logging
import re

from . import hooks as hook_library
from . import style_template

logger = logging.getLogger(__name__)


class CopywritingError(RuntimeError):
    pass


# ------------------------------------------------------------------ 房源事实

def _norm_facts(facts: dict) -> dict[str, str]:
    """房源字段统一成中文 key。中英文都认——接口层给的是中文（区域/面积/租金），
    老调用方给的是英文（region/area/rent），两边都不能丢字段。"""
    keys = {
        "region": "区域", "building": "楼栋", "room": "房号", "area": "面积",
        "partition": "格局", "orientation": "朝向", "furnishing_layout": "家具",
        "rent": "租金", "title": "名称", "notes": "备注",
    }
    # 同一个字段在别处可能叫另一个中文名，一起认
    aliases = {"家具": ["家具", "家具装修"]}
    out: dict[str, str] = {}
    for key, label in keys.items():
        value = facts.get(key)
        if not value:
            for name in aliases.get(label, [label]):
                value = facts.get(name)
                if value:
                    break
        text = str(value or "").strip()
        if text:
            out[label] = text
    return out


def _area_number(facts: dict[str, str]) -> str:
    match = re.search(r"\d+(?:\.\d+)?", facts.get("面积", ""))
    return match.group(0) if match else ""


# ------------------------------------------------------------------ 规则套写

# 句式库按「话题」分组，生成时按模板原句的关键词路由，句句贴题不重样
_LINE_BANK: dict[str, list[str]] = {
    "hook": [
        "今天带大家看一套{area}平的办公室", "实地看房，{area}平精装办公直接看",
        "老板们看过来，{area}平办公室实拍",
    ],
    "area": [
        "{area}平，{partition}的办公室", "面积{area}平，{partition}格局",
        "{area}平{partition}，大小刚刚好",
        # 钩子里已经报过面积时改说格局，不然开头和这句像复读
        "格局方正，没有斜角", "方方正正，家具怎么摆都合适",
    ],
    "light": [
        "采光通透，全景落地窗", "整整一面落地窗，白天不用开灯",
        "双面采光，下午都不用开灯", "这采光，隔着屏幕都亮堂",
    ],
    "furn": [
        # 这四句讲的是四个不同角度（齐全 / 省钱 / 归属 / 状态）。
        # 一镜到底的片子经常有两镜都是家具，连着说时不能两句都在说"全都有"。
        "工桌工椅全套配齐，直接开工", "桌椅沙发全配好，一分钱不用花",
        "家具全送，到期不用带走", "带全套家具，拎包就能办公",
    ],
    "boss": [
        "独立老板间，谈单不吵", "单独一间老板办公室，会客安静",
        "老板间带全套家具，拎包办公",
    ],
    "zone": [
        "独立工区，工位随到随摆", "前台、洽谈区、会议室一应俱全",
        "入户就是工区，动线特别顺", "能摆下整套工位，还富余",
    ],
    "value": [
        "一个月{monthly}，摊到每天{daily}元", "{seats}个工位，人均一个月{per_person}元",
        "这个价格拿下这一层，真不多见", "即入驻即办公，空置期一天不浪费",
        "算下来比换个地方重新装修划算多了",
    ],
    "cta": [
        "感兴趣的老板，赶紧过来看看吧", "心动的老板私信我，约时间实地看",
        "有意向直接联系我，随时带看",
    ],
}

# 模板原句关键词 → 话题库（按优先级排）
_KEYWORD_ROUTING: list[tuple[str, str]] = [
    ("采光", "light"), ("落地窗", "light"),
    ("桌", "furn"), ("椅", "furn"), ("沙发", "furn"), ("家具", "furn"), ("俱全", "furn"),
    ("老板", "boss"), ("洽谈", "boss"),
    ("工区", "zone"), ("工位", "zone"), ("前台", "zone"), ("接待", "zone"), ("入户", "zone"),
    ("租", "value"), ("入住", "value"), ("价", "value"),
]


def _topic_for(sample: str, role: str) -> str:
    # 没模板时我们自己造的 role 就是话题名，直接命中
    role_key = str(role or "").strip().lower()
    if role_key in _LINE_BANK:
        return role_key
    text = f"{sample} {role}"
    for keyword, topic in _KEYWORD_ROUTING:
        if keyword in text:
            return topic
    return "light"


def _fill(pattern: str, facts: dict[str, str]) -> str | None:
    """套房源变量。缺字段就返回 None，宁可少一句也不出半截话。"""
    ctx = hook_library.context(facts)
    rent = facts.get("租金", "")
    ctx.setdefault("labeled", rent or "合适的价格")
    try:
        return pattern.format(**ctx)
    except (KeyError, IndexError):
        return None


def fallback_storyboard(
    spec: dict, facts: dict, *, audience: str = "any", hook: str = ""
) -> list[dict]:
    """规则套写：按模板每句的话题路由句式库，套房源字段。

    audience 只影响第一句——开头是给谁看的，后面几句讲的都是房子本身。
    hook 传了就用它当第一句（前端三版里选中的那条）。
    """
    normalized = _norm_facts(facts)
    shots = spec.get("shots") or []
    if not shots:
        raise CopywritingError("模板里没有分镜结构，无法生成文案")

    used: set[str] = set()
    board: list[dict] = []
    # 每个话题库记一个游标轮转取句，尽量少回头
    cursors: dict[str, int] = {}

    def pick(topic: str, *, avoid: tuple[str, ...] = ()) -> str:
        bank = _LINE_BANK[topic]
        # 开头已经报过的数字不许再报一次，换说法的句子顶上
        for name in avoid:
            token = "{" + name + "}"
            lean = [p for p in bank if token not in p]
            if lean:
                bank = lean
        start = cursors.get(topic, 0)
        for offset in range(len(bank)):
            pattern = bank[(start + offset) % len(bank)]
            # 句式依赖租金但房源没给租金，跳过这句式，别说出「租金合适的价格」
            if "租金" in pattern and not normalized.get("租金"):
                continue
            line = _fill(pattern, normalized)
            if line and line not in used:
                cursors[topic] = (start + offset + 1) % len(bank)
                used.add(line)
                return line
        # 这个库全用过了：全局找一句没说过的
        for other_topic, other_bank in _LINE_BANK.items():
            if other_topic == topic:
                continue
            for pattern in other_bank:
                line = _fill(pattern, normalized)
                if line and line not in used:
                    used.add(line)
                    return line
        return _fill(bank[0], normalized) or ""

    # 开头优先用钩子库（三档话术），没有库才退回老句式
    opening = str(hook or "").strip()
    if not opening:
        picked = hook_library.pick(normalized, audience, count=1)
        opening = picked[0]["text"] if picked else ""
    area_number = _area_number(normalized)
    area_used = bool(area_number and area_number in opening)
    opening_ctx = hook_library.context(normalized)
    daily_used = bool(opening_ctx.get("daily") and opening_ctx["daily"] in opening)
    monthly_used = bool(opening_ctx.get("monthly") and opening_ctx["monthly"] in opening)

    for position, shot in enumerate(shots):
        role = str(shot.get("role") or "")
        sample = str(shot.get("sample") or "")
        if position == 0:
            text = opening or pick("hook")
        elif "面积" in role or "area" in role.lower():
            text = pick("area", avoid=("area",) if area_used else ())
        elif "cta" in role.lower() or position == len(shots) - 1:
            text = pick("cta")
        elif _topic_for(sample, role) == "value":
            skip = tuple(name for name, used in (("daily", daily_used), ("monthly", monthly_used)) if used)
            text = pick("value", avoid=skip)
        else:
            text = pick(_topic_for(sample, role))
        if not text:
            continue
        # 开头已经用过的不许再出现第二次
        if position == 0:
            used.add(text)
        entry = {"index": len(board) + 1, "text": text}
        label = shot.get("labels") or []
        if label:
            entry["label"] = {"title": str(label[0])[:6]}
        board.append(entry)
    return board


# 没有参考模板时的默认分镜：钩子 → 面积 → 公区 → 家具 → 价格 → 行动
DEFAULT_ROLES = ("hook", "area", "zone", "furn", "value", "cta")


def default_spec(shot_count: int = 6) -> dict:
    """没拆过参考视频也能出片：用一个行业通行的六镜骨架。"""
    roles = list(DEFAULT_ROLES)[:max(3, min(shot_count, len(DEFAULT_ROLES)))]
    return {
        "shots": [{"index": i + 1, "role": role, "sample": ""} for i, role in enumerate(roles)],
        "constraints": {"max_words_per_shot": 18},
        "source": "default",
    }


# ------------------------------------------------------------------ 大模型

COPY_PROMPT = """你在给一条办公室租赁短视频写逐句口播文案。

参考模板（学它的结构、句数、节奏和口吻，不要抄它的内容）：
{spec}

这套房源的可用信息（只能用这些，不许编造面积、租金、朝向、配套）：
{facts}

开头三选一（本地钩子库按这条房源算出来的，第一句必须用其中一版，允许改语气，不许换意思）：
{hooks}

这条片子拍给「{audience}」看，用他们自己的说法，别用中介内部术语。

要求：
1. 输出 {count} 句，一镜一句，角色顺序跟模板 shots 一致。
2. 每句不超过 {limit} 字；中文口播每秒约 4 字，宁可短不要长。
3. 相邻两句不许意思重复；数字钩子（面积、租金）最多出现一次，别句句报数。
4. 至少有一句要把月租折成「一天多少钱」或「一个人多少钱」，老板听这个才有感觉。
5. 最后一句必须是行动号召。

只输出 JSON：{{"shots": [{{"index": 1, "role": "hook", "text": "第一句"}}]}}"""


def _audience_label(key: str) -> str:
    for item in hook_library.audiences():
        if item.get("key") == key:
            return str(item.get("label") or key)
    return "不限人群"


def _dedupe(board: list[dict]) -> list[dict]:
    """相邻句去重 + 全文去重：大模型偶尔会把同一句话换个语气再说一遍。"""
    seen: set[str] = set()
    out: list[dict] = []
    for entry in board:
        text = re.sub(r"[，。！？、\s]", "", entry["text"])
        if not text or text in seen:
            continue
        # 相似度兜底：前 8 字相同也算重复
        if any(text[:8] == old[:8] for old in seen):
            continue
        seen.add(text)
        entry["index"] = len(out) + 1
        out.append(entry)
    return out


def llm_storyboard(spec: dict, facts: dict, *, audience: str = "any", hook: str = "") -> list[dict]:
    """调火山方舟按模板写文案。没配 key 时抛错，由调用方决定兜底。"""
    if not style_template.is_configured():
        raise CopywritingError("未配置 ARK_API_KEY")
    normalized = _norm_facts(facts)
    shots = spec.get("shots") or []
    limit = int((spec.get("constraints") or {}).get("max_words_per_shot") or 20)

    if hook:
        picked = [hook]
    else:
        picked = [item["text"] for item in hook_library.pick(normalized, audience, count=3)]

    prompt = COPY_PROMPT.format(
        spec=json.dumps(spec, ensure_ascii=False),
        facts="\n".join(f"{k}：{v}" for k, v in normalized.items()) or "（无）",
        hooks="\n".join(f"- {text}" for text in picked) or "（房源信息太少，自己按面积写一句）",
        audience=_audience_label(audience),
        count=len(shots),
        limit=limit,
    )
    config = style_template.style_config()
    result = style_template._chat([{"role": "user", "content": prompt}], config["text_model"])
    board = []
    for item in result.get("shots") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        entry = {"text": text}
        label = item.get("label") or {}
        if isinstance(label, dict) and label.get("title"):
            entry["label"] = {"title": str(label["title"])[:6], "sub": str(label.get("sub", ""))[:20]}
        board.append(entry)
    board = _dedupe(board)
    if not board:
        raise CopywritingError("模型没有生成可用文案")
    return board


def generate_storyboard(
    spec: dict,
    facts: dict,
    *,
    prefer_api: bool = True,
    audience: str = "any",
    hook: str = "",
    shot_count: int = 6,
) -> tuple[list[dict], str]:
    """统一入口。返回 (storyboard, 来源)；来源为 "api" 或 "rules"。

    没给模板（或模板里没有分镜）时用六镜默认骨架，让没拆过参考视频的
    房源也能直接出片——以前这种情况是直接抛错的。
    """
    if not (spec or {}).get("shots"):
        spec = default_spec(shot_count)
    if prefer_api:
        try:
            return llm_storyboard(spec, facts, audience=audience, hook=hook), "api"
        except Exception as error:  # noqa: BLE001 - API 失败安静地退规则
            logger.warning("API 文案失败，退回规则套写：%s", str(error)[:200])
    return fallback_storyboard(spec, facts, audience=audience, hook=hook), "rules"


# ------------------------------------------------------------------ 发布标题

def build_titles(facts: dict, audience: str = "any") -> dict[str, list[str]]:
    """同一套房源，两个平台两套标题。

    抖音靠前两秒决定划不划走，标题要把面积和价格顶到最前面；
    小红书是搜索场，标题得能被「XX 区 办公室 出租」这种词搜到。
    """
    ctx = hook_library.context(facts)

    def fmt(tpl: str) -> str | None:
        needed = re.findall(r"\{(\w+)\}", tpl)
        if any(not ctx.get(name) for name in needed):
            return None
        return tpl.format(**ctx)

    douyin = [
        fmt("{region}{area}平办公室，{monthly}一个月"),
        fmt("{area}平{partition}，一天{daily}元，老板来算笔账"),
        fmt("在{region}找{area}平的看过来，{monthly}能拿下"),
        fmt("{area}平带全套家具，月租{monthly}"),
        fmt("{region}办公室出租，{area}平，{monthly}"),
        # 租金缺失或算不出来时的兜底：标题不能空着
        fmt("{region}{area}平办公室，{partition}"),
        fmt("{area}平，这个格局搬进来就能开工"),
    ]
    xhs = [
        fmt("{region}看房｜{area}平精装{partition}"),
        fmt("{area}平｜家具全带，拎包就开工"),
        fmt("找办公室｜{region}{area}平，一天{daily}元"),
        fmt("月租{monthly}｜{area}平，够{seats}个工位"),
        fmt("找办公室先看这间｜{region}{area}平"),
        fmt("{region}办公室｜{area}平，格局很正"),
    ]

    return {
        # 抖音标题超过 30 字会被折，小红书超过 20 字会被截
        "douyin": _tidy(douyin, 30),
        "xiaohongshu": _tidy(xhs, 20),
        "audience": _audience_label(audience),
    }


def _tidy(rows: list[str | None], limit: int) -> list[str]:
    out: list[str] = []
    for text in rows:
        text = (text or "").strip()
        if not text or len(text) > limit or text in out:
            continue
        out.append(text)
    return out[:3]


def join_script(board: list[dict]) -> str:
    """分镜拼成一篇口播稿：每句一个句号，念的时候才有停顿。"""
    parts = [
        str(entry.get("text") or "").strip().rstrip("。！？，、")
        for entry in board
    ]
    return "。".join(part for part in parts if part) + "。"
