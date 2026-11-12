"""大模型接入：豆包（火山方舟）/ DeepSeek，统一成一套 chat 调用。

两家都是 OpenAI 兼容的 /chat/completions，只有 base_url、model 和 key 不同，
所以一个薄客户端就够，不引入各家 SDK。

配置优先级：系统设置（settings 表）> 环境变量。
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from .db import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


# 常见组合的默认值，用户在设置里只填 key 就能用
PROVIDERS: dict[str, dict[str, str]] = {
    "ark": {
        "label": "豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-1-6-250615",
        "env_key": "ARK_API_KEY",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
    },
}


def config() -> dict:
    """当前生效的模型配置。provider 为空表示没配。"""
    settings = get_settings()
    provider = (settings.get("llm_provider") or "").strip().lower()
    if provider not in PROVIDERS:
        provider = ""
    preset = PROVIDERS.get(provider) or {}
    api_key = (settings.get("llm_api_key") or "").strip()
    if not api_key and preset.get("env_key"):
        api_key = os.getenv(preset["env_key"], "").strip()
    return {
        "provider": provider,
        "label": preset.get("label", ""),
        "api_key": api_key,
        "base_url": ((settings.get("llm_base_url") or "").strip() or preset.get("base_url", "")).rstrip("/"),
        "model": (settings.get("llm_model") or "").strip() or preset.get("model", ""),
        "ready": bool(provider and api_key and preset.get("base_url")),
    }


def status() -> dict:
    info = config()
    return {
        "provider": info["provider"],
        "label": info["label"],
        "model": info["model"],
        "ready": info["ready"],
        "hint": "" if info["ready"] else "在系统设置里选一个模型服务商并填 API Key，就能用 AI 写文案",
    }


def chat(messages: list[dict], *, temperature: float = 0.7, timeout: float = 120.0) -> str:
    info = config()
    if not info["ready"]:
        raise LLMError("还没配置大模型：系统设置里选服务商并填 API Key")
    body = {"model": info["model"], "messages": messages, "temperature": temperature}
    try:
        with httpx.Client(
            base_url=info["base_url"],
            timeout=httpx.Timeout(timeout, connect=15),
            headers={"Authorization": f"Bearer {info['api_key']}"},
            trust_env=False,
        ) as client:
            response = client.post("/chat/completions", json=body)
    except httpx.HTTPError as error:
        raise LLMError(f"连不上 {info['label']}：{error}") from error
    if response.status_code >= 400:
        detail = response.text[:300]
        if response.status_code in (401, 403):
            raise LLMError(f"{info['label']} 鉴权失败，检查 API Key 是否正确（{detail}）")
        raise LLMError(f"{info['label']} 调用失败 {response.status_code}：{detail}")
    try:
        return str(response.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise LLMError(f"{info['label']} 返回结构异常：{response.text[:200]}") from error


# ------------------------------------------------------------------ 提示词

WRITE_SYSTEM = """你是给办公室租赁短视频写口播的文案，风格像中介带看时说话：
短句、口语、面对面，不念参数清单，每句都有信息量。
硬性要求：
- 只用给定信息，不许编造面积、租金、朝向、楼层景观、周边配套；
- 不出现价格承诺、联系方式诱导以外的敏感信息；
- 每句 10~22 字，以句号结尾，一句一行；
- 只输出文案正文，不要编号、不要解释、不要标题。"""

POLISH_SYSTEM = """你是短视频口播文案编辑。把用户给的稿子改成更适合念出来的口播：
- 保留全部事实，不许新增面积、租金、朝向等任何信息；
- 句子改短，一句一个意思，每句 10~22 字，以句号结尾，一句一行；
- 去掉书面腔和重复句，加一点对话感（可以用「你」「老板」）；
- 只输出改好的文案正文，不要解释。"""


def _facts_text(facts: dict) -> str:
    lines = [f"{key}：{value}" for key, value in facts.items() if value]
    return "\n".join(lines) or "（房源信息很少，别编，多用带看邀约和判断）"


def _sentence_budget(target_duration: int) -> tuple[int, int]:
    """按目标时长换算句子数：中文口播约 4.2 字/秒，一句 15 字上下。"""
    seconds = max(10, int(target_duration or 45))
    count = round(seconds * 4.2 / 16)
    return max(4, count - 2), max(6, count + 2)


def write_script(facts: dict, target_duration: int = 45, extra: str = "") -> str:
    low, high = _sentence_budget(target_duration)
    hint = f"\n额外要求（优先满足，但不能违反上面的事实限制）：{extra.strip()}" if extra.strip() else ""
    prompt = f"""写一条 {target_duration} 秒的办公室带看口播。

房源信息：
{_facts_text(facts)}

句数：{low}~{high} 句（这是按 {target_duration} 秒配的，别写少也别写超）。
结构建议：开场钩子 → 位置 → 面积（可换算成能摆多少工位，约 7.5~10.5 平一个工位）→ 格局怎么用 → 亮点（朝向/家具，没有就跳过）→ 适合谁 → 邀约带看 → 收尾。
最后一句必须是引导私信或评论的行动号召。{hint}"""
    text = chat(
        [{"role": "system", "content": WRITE_SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.8,
    )
    return normalize_script(text)


def polish_script(script: str, facts: dict | None = None) -> str:
    prompt = f"""把下面这段文案改成口播稿。

原文：
{script.strip()}

房源信息（只用来核对事实，不要往里加东西）：
{_facts_text(facts or {})}"""
    text = chat(
        [{"role": "system", "content": POLISH_SYSTEM}, {"role": "user", "content": prompt}],
        temperature=0.5,
    )
    return normalize_script(text)


def normalize_script(text: str) -> str:
    """把模型输出收拾成「一句一行、句号收尾」的干净文案。"""
    text = (text or "").strip()
    if not text:
        raise LLMError("模型返回了空文案")
    # 去掉常见的包裹：```、编号、markdown 列表符
    text = re.sub(r"```[a-zA-Z]*", "", text).strip("` \n")
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.、)]|第[一二三四五六七八九十]+句[:：]?)\s*", "", raw).strip()
        line = line.strip("　 \"'“”")
        if line:
            lines.append(line)
    text = "".join(lines)
    # 统一断句：按句末标点切开，再一句一行
    pieces = [p.strip() for p in re.split(r"(?<=[。！？!?])", text) if p.strip()]
    out = []
    for piece in pieces:
        if not re.search(r"[。！？!?]$", piece):
            piece += "。"
        out.append(piece)
    result = "".join(out)
    if len(result) < 20:
        raise LLMError("模型文案太短，换一次试试")
    return result
