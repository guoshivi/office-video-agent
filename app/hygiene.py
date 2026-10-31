"""口播文案的措辞卫生。

真实事故催出来的一个模块：两条已经出好的片子里，配音念出了
「租金和物业费口径请以最新房源确认为准」。这句话是写给自己人和
同事看的——内部对数据口径有争议，所以加个提醒。但被念出来，
就变成了对客户说「我们的价格不确定」，等于自己拆自己的台。

两个地方要用它：
- 出片前（`workbench_client`）：把这类句子从口播里摘掉，别等渲染完才发现；
- 审核时（`review.precheck`）：发现成片里还有，就拦下来。

只摘整句，不改写剩下的内容——改文案是写文案的人的事，
自动改写会把「一百二十平」这种事实也一起动了。
"""

from __future__ import annotations

import re

# 这些词一出现，整句都不该对外说。宁可少一句，也别把内部语言播出去。
INTERNAL_PHRASES = (
    "口径",
    "请以最新",
    "以最新房源确认",
    "以实际为准",
    "内部",
    "备注",
    "待确认",
    "需确认",
    "仅供参考",
    "不对外",
    "自动生成",
    "AI 生成",
)

# 按中文句末标点断句，保留标点本身
_SENTENCE = re.compile(r"[^。！？!?\n]*[。！？!?\n]?")


def find_internal_phrases(text: str) -> list[str]:
    """列出文本里出现的内部用语（去重、保持出现顺序）。"""
    if not text:
        return []
    seen: list[str] = []
    for phrase in INTERNAL_PHRASES:
        if phrase in text and phrase not in seen:
            seen.append(phrase)
    return seen


def split_sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE.findall(text or "") if part.strip()]


def clean_internal_sentences(text: str) -> tuple[str, list[str]]:
    """摘掉带内部用语的整句，返回 (清理后的文本, 被摘掉的句子)。

    如果整段都带内部用语（清理后什么都不剩），原样返回——
    让工作台出个有瑕疵的片子，比让它收到空文案直接报错要好，
    反正审核台还会再拦一次。
    """
    original = (text or "").strip()
    if not original:
        return "", []

    kept: list[str] = []
    removed: list[str] = []
    for sentence in split_sentences(original):
        if find_internal_phrases(sentence):
            removed.append(sentence.strip())
        else:
            kept.append(sentence)

    cleaned = "".join(kept).strip()
    # 句子被摘掉后可能留下连续换行
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    if not removed or not cleaned:
        return original, []
    return cleaned, removed
