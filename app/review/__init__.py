"""审核台：出片之后、发布之前的那一道关。

模块边界：
- precheck.py  机器能判断的事（事实比对、合规、成片体检、重复发布）
- store.py     审核队列的持久化（只读写，不做决策）
- service.py   编排（入队、预检、通过/驳回、是否自动发）
- bitable_bridge.py  可选的多维表格镜像，没配凭据就纯本地跑

设计取舍：审核台**不依赖飞书也能完整使用**。多维表格只是另一个操作入口，
不是这套东西的前置条件——朋友在手机上用飞书审、在电脑上用 WebUI 审，
看到的是同一份队列。
"""

from .precheck import (
    Check,
    PrecheckReport,
    cn_to_int,
    extract_areas,
    extract_rents,
    precheck,
    probe_streams,
)
from .service import (
    STATUS_LABELS,
    ReviewError,
    approve,
    detail,
    list_queue,
    publish_now,
    refresh_queue,
    reject,
    run_precheck,
    skip,
)
from . import store

__all__ = [
    "STATUS_LABELS",
    "Check",
    "PrecheckReport",
    "ReviewError",
    "approve",
    "cn_to_int",
    "detail",
    "extract_areas",
    "extract_rents",
    "list_queue",
    "precheck",
    "probe_streams",
    "publish_now",
    "refresh_queue",
    "reject",
    "run_precheck",
    "skip",
    "store",
]
