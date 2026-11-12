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

from . import style_template

logger = logging.getLogger(__name__)


class CopywritingError(RuntimeError):
    pass


# ------------------------------------------------------------------ 房源事实

def _norm_facts(facts: dict) -> dict[str, str]:
    keys = {
        "region": "区域", "building": "楼栋", "room": "房号", "area": "面积",
        "partition": "格局", "orientation": "朝向", "furnishing_layout": "家具",
        "rent": "租金", "title": "名称", "notes": "备注",
    }
    out: dict[str, str] = {}
    for key, label in keys.items():
        value = str(facts.get(key) or "").strip()
        if value:
            out[label] = value
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
    ],
    "light": [
        "采光通透，全景落地窗", "整整一面落地窗，白天不用开灯",
        "双面采光，下午都不用开灯", "这采光，隔着屏幕都亮堂",
    ],
    "furn": [
        "工桌工椅全套配齐，直接开工", "带全套家具，拎包就能办公",
        "桌椅沙发全配好，一分钱不用花", "家具全送，到期不用带走",
    ],
    "boss": [
        "独立老板间，谈单不吵", "老板间带全套家具，拎包办公",
        "单独一间老板办公室，会客安静",
    ],
    "zone": [
        "独立工区，工位随到随摆", "前台、洽谈区、会议室一应俱全",
        "入户就是工区，动线特别顺", "能摆下整套工位，还富余",
    ],
    "value": [
        "租金{labeled}，性价比直接拉满", "这个价格拿下这一层，真不多见",
        "即入驻即办公，空置期一天不浪费",
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
    text = f"{sample} {role}"
    for keyword, topic in _KEYWORD_ROUTING:
        if keyword in text:
            return topic
    return "light"


def _fill(pattern: str, facts: dict[str, str]) -> str:
    area = _area_number(facts)
    rent = facts.get("租金", "")
    labeled = f"{rent}" if rent else "合适的价格"
    return pattern.format(area=area, partition=facts.get("格局", "精装"), labeled=labeled)


def fallback_storyboard(spec: dict, facts: dict) -> list[dict]:
    """规则套写：按模板每句的话题路由句式库，套房源字段。"""
    normalized = _norm_facts(facts)
    shots = spec.get("shots") or []
    if not shots:
        raise CopywritingError("模板里没有分镜结构，无法生成文案")

    used: set[str] = set()
    board: list[dict] = []
    # 每个话题库记一个游标轮转取句，尽量少回头
    cursors: dict[str, int] = {}

    def pick(topic: str) -> str:
        bank = _LINE_BANK[topic]
        start = cursors.get(topic, 0)
        for offset in range(len(bank)):
            pattern = bank[(start + offset) % len(bank)]
            # 句式依赖租金但房源没给租金，跳过这句式，别说出「租金合适的价格」
            if "租金" in pattern and not normalized.get("租金"):
                continue
            line = _fill(pattern, normalized)
            if line not in used:
                cursors[topic] = (start + offset + 1) % len(bank)
                used.add(line)
                return line
        # 这个库全用过了：全局找一句没说过的
        for other_topic, other_bank in _LINE_BANK.items():
            if other_topic == topic:
                continue
            for pattern in other_bank:
                line = _fill(pattern, normalized)
                if line not in used:
                    used.add(line)
                    return line
        return _fill(bank[0], normalized)

    for position, shot in enumerate(shots):
        role = str(shot.get("role") or "")
        sample = str(shot.get("sample") or "")
        if position == 0 or "hook" in role.lower():
            text = pick("hook")
        elif "面积" in role or "area" in role.lower():
            text = pick("area")
        elif "cta" in role.lower() or position == len(shots) - 1:
            text = pick("cta")
        else:
            text = pick(_topic_for(sample, role))
        if not text:
            continue
        entry = {"index": len(board) + 1, "text": text}
        label = shot.get("labels") or []
        if label:
            entry["label"] = {"title": str(label[0])[:6]}
        board.append(entry)
    return board


# ------------------------------------------------------------------ 大模型

COPY_PROMPT = """你在给一条办公室租赁短视频写逐句口播文案。

参考模板（学它的结构、句数、节奏和口吻，不要抄它的内容）：
{spec}

这套房源的可用信息（只能用这些，不许编造面积、租金、朝向、配套）：
{facts}

要求：
1. 输出 {count} 句，一镜一句，角色顺序跟模板 shots 一致。
2. 每句不超过 {limit} 字；中文口播每秒约 4 字，宁可短不要长。
3. 相邻两句不许意思重复；数字钩子（面积、租金）最多出现一次，别句句报数。
4. 最后一句必须是行动号召。

只输出 JSON：{{"shots": [{{"index": 1, "role": "hook", "text": "第一句"}}]}}"""


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


def llm_storyboard(spec: dict, facts: dict) -> list[dict]:
    """调火山方舟按模板写文案。没配 key 时抛错，由调用方决定兜底。"""
    if not style_template.is_configured():
        raise CopywritingError("未配置 ARK_API_KEY")
    normalized = _norm_facts(facts)
    shots = spec.get("shots") or []
    limit = int((spec.get("constraints") or {}).get("max_words_per_shot") or 20)
    prompt = COPY_PROMPT.format(
        spec=json.dumps(spec, ensure_ascii=False),
        facts="\n".join(f"{k}：{v}" for k, v in normalized.items()) or "（无）",
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


def generate_storyboard(spec: dict, facts: dict, *, prefer_api: bool = True) -> tuple[list[dict], str]:
    """统一入口。返回 (storyboard, 来源)；来源为 "api" 或 "rules"。"""
    if prefer_api:
        try:
            return llm_storyboard(spec, facts), "api"
        except Exception as error:  # noqa: BLE001 - API 失败安静地退规则
            logger.warning("API 文案失败，退回规则套写：%s", str(error)[:200])
    return fallback_storyboard(spec, facts), "rules"
