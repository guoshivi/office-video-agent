"""飞书入口：长连接收消息 → 下载原片 → 建任务 → 回传口播与成片。"""

from .client import FeishuClient, FeishuConfig, FeishuError

__all__ = ["FeishuClient", "FeishuConfig", "FeishuError"]
