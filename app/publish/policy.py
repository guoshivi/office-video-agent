"""发布前的硬性规则校验。

平台规则是硬的，不校验就会踩到：
- 小红书标题上限 20 字，超了要么报错要么被截断得莫名其妙；
- 正文上限 1000 字；
- 正文里出现微信号/手机号属于"引流"，官方重点打击，轻则限流重则封号；
- 小红书的浏览器自动化对**非 ASCII 路径**很敏感（README 排障第一条就是"确认路径中没有中文字符"）。

所以发布前统一做两件事：把成片 stage 到一个纯 ASCII 的短路径，
再按平台规则把文案裁到合规长度并挑出风险词。

校验结果分两级：
- blocking：不改就不能发（超长、无视频、命中引流词）；
- warn：能发但风险高（绝对化用语），交给发布者决定。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# 平台硬限制。抖音的标题其实可以更长，但 feed 里只展示一部分，按经验值收敛。
XHS_TITLE_MAX = 20
XHS_CONTENT_MAX = 1000
DOUYIN_TITLE_MAX = 30
DOUYIN_CONTENT_MAX = 2000

TITLE_MAX = {"xiaohongshu": XHS_TITLE_MAX, "douyin": DOUYIN_TITLE_MAX}
CONTENT_MAX = {"xiaohongshu": XHS_CONTENT_MAX, "douyin": DOUYIN_CONTENT_MAX}

# 引流：真正会触发风控的是把联系方式写在正文里。
# "私信咨询"属于平台允许的互动引导，只提示不拦——否则默认的 public_contact
# （"欢迎私信咨询房源详情和看房安排"）会直接被自己的规则挡死。
_BLOCK_CONTACT = [
    (re.compile(r"微信|薇信|威信|v\s*信|vx|wx|weixin", re.I), "微信号"),
    (re.compile(r"\bqq\b|扣扣", re.I), "QQ"),
    (re.compile(r"1[3-9]\d{9}"), "手机号"),
    (re.compile(r"加\s*我|加\s*[vV]|私我拿|私我领取"), "引导加好友"),
]

_WARN_CONTACT = [
    (re.compile(r"私信我|私聊我|私我"), "强引导私聊"),
]

# 广告法 + 平台限流双重风险。中介文案最容易写"最低价""保证入驻"。
_ABSOLUTE_WORDS = (
    "最低价",
    "最便宜",
    "全城最低",
    "第一",
    "唯一",
    "顶级",
    "独家",
    "保证",
    "包过",
    "稳赚",
    "绝对",
    "百分百",
)

# 项目自己的输出目录本来就是 ASCII，但素材根目录可能是中文（Z:\下载\...）。
# 小红书那边对中文路径的容忍度很低，统一复制到 ASCII staging 目录最省事。
STAGING_SUBDIR = "publish_stage"


@dataclass
class Issue:
    level: str  # blocking | warn
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "message": self.message}


@dataclass
class CheckResult:
    issues: list[Issue] = field(default_factory=list)
    title: str = ""
    content: str = ""
    video_path: str = ""

    @property
    def blocking(self) -> list[Issue]:
        return [item for item in self.issues if item.level == "blocking"]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "title": self.title,
            "content": self.content,
            "video_path": self.video_path,
            "issues": [item.as_dict() for item in self.issues],
        }


def is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def stage_video(source: Path, staging_root: Path, stem: str) -> Path:
    """把成片复制到一个纯 ASCII、无空格、短路径的位置。

    发布用的是复制而不是原路径，原因有三个：
    1. 小红书侧对非 ASCII 路径不稳定（README 明确列为排障项）；
    2. 原路径可能在工作台授权根目录里，可能被清理或替换；
    3. 上传耗时期间原文件被改写会得到一个坏的上传。
    """
    staging_root.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem) or "video"
    target = staging_root / f"{safe_stem}.mp4"
    if target.exists() and target.stat().st_size == source.stat().st_size:
        return target
    shutil.copy2(source, target)
    return target


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    # 优先在句末切开，实在找不到就硬截，避免把一句话砍成半截。
    window = text[:limit]
    for mark in ("。", "！", "？", "；", "\n", "，"):
        cut = window.rfind(mark)
        if cut >= limit * 0.6:
            return window[: cut + 1].strip()
    return window.strip()


def scan_text(text: str) -> list[Issue]:
    issues: list[Issue] = []
    for pattern, label in _BLOCK_CONTACT:
        if pattern.search(text or ""):
            issues.append(Issue("blocking", f"文案里出现{label}，小红书和抖音都会判引流"))
    for pattern, label in _WARN_CONTACT:
        if pattern.search(text or ""):
            issues.append(Issue("warn", f"出现「{label}」，可能被限流，建议改成「主页联系」"))
    hits = [word for word in _ABSOLUTE_WORDS if word in (text or "")]
    if hits:
        issues.append(
            Issue("warn", "绝对化用语：" + "、".join(hits) + "（广告法风险，建议换中性说法）")
        )
    return issues


def check_xhs(title: str, content: str, video: Path) -> CheckResult:
    result = CheckResult()
    result.title = _clip(title, XHS_TITLE_MAX)
    result.content = _clip(content, XHS_CONTENT_MAX)

    if not title or not title.strip():
        result.issues.append(Issue("blocking", "标题为空"))
    elif len(title.strip()) > XHS_TITLE_MAX:
        result.issues.append(
            Issue("warn", f"标题 {len(title.strip())} 字，已裁到 {XHS_TITLE_MAX} 字")
        )

    if not content or not content.strip():
        result.issues.append(Issue("blocking", "正文为空"))
    elif len(content.strip()) > XHS_CONTENT_MAX:
        result.issues.append(Issue("warn", f"正文超长，已裁到 {XHS_CONTENT_MAX} 字"))

    if not video.is_file():
        result.issues.append(Issue("blocking", f"成片不存在：{video}"))
    elif video.stat().st_size < 1024:
        result.issues.append(Issue("blocking", "成片文件过小，疑似空文件"))

    result.issues.extend(scan_text(result.content))
    result.issues.extend(scan_text(result.title))
    return result
