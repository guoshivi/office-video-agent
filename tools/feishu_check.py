"""飞书凭据自检。

用法：
    python tools/feishu_check.py

按顺序验证：凭据是否存在 → 能否拿到 tenant_access_token → 机器人是否开启 →
权限是否够（能否读群列表）→ 多维表格是否可用。每一步失败都会给出具体原因，
不用去猜是权限、发布还是配置的问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.feishu.client import FeishuClient, FeishuConfig, FeishuError  # noqa: E402


def step(index: int, title: str) -> None:
    print(f"\n[{index}] {title}")


def ok(message: str) -> None:
    print(f"    OK   {message}")


def fail(message: str, hint: str = "") -> None:
    print(f"    FAIL {message}")
    if hint:
        print(f"         → {hint}")


def main() -> int:
    config = FeishuConfig.from_env()
    client = FeishuClient(config)
    failures = 0

    step(1, "读取 .env 凭据")
    if not config.has_credentials:
        fail("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET", "见 docs/feishu-setup.md 第一步")
        return 1
    ok(f"App ID = {config.app_id[:8]}…（长度 {len(config.app_id)}）")

    step(2, "获取 tenant_access_token")
    try:
        client._access_token()
        ok("令牌获取成功，说明 App ID / Secret 正确且应用未被停用")
    except FeishuError as error:
        fail(str(error), "检查 Secret 是否复制完整、应用是否已发布")
        return 1

    step(3, "读取机器人信息")
    try:
        payload = client.get_json("/open-apis/bot/v3/info")
        bot = payload.get("bot") or {}
        ok(f"机器人名称：{bot.get('app_name') or '（未命名）'}")
        if not bot.get("activate_status"):
            print("         注意：机器人能力可能未开启 → 添加应用能力 → 机器人")
    except FeishuError as error:
        fail(str(error), '缺少 im:message 权限，或未开启「机器人」应用能力')
        failures += 1

    step(4, "读取机器人所在会话（验证 im:chat 权限）")
    try:
        payload = client.get_json("/open-apis/im/v1/chats", {"page_size": 10})
        items = payload.get("data", {}).get("items") or []
        ok(f"可访问 {len(items)} 个会话")
        for item in items[:10]:
            print(f"         · {item.get('name') or '（未命名）'}  chat_id={item.get('chat_id')}")
        if not items:
            print("         机器人还没被拉进任何群，单聊直接搜索机器人名称即可")
    except FeishuError as error:
        fail(str(error), "缺少 im:chat 权限（只影响群聊，单聊仍可用）")

    step(5, "检查多维表格配置")
    if not config.has_bitable:
        print("    跳过：未配置 FEISHU_BASE_APP_TOKEN / FEISHU_TASK_TABLE_ID")
    else:
        try:
            payload = client.get_json(
                f"/open-apis/bitable/v1/apps/{config.base_app_token}"
                f"/tables/{config.task_table_id}/fields",
                {"page_size": 50},
            )
            names = [f.get("field_name") for f in payload.get("data", {}).get("items") or []]
            ok(f"表字段 {len(names)} 个：{'、'.join(str(n) for n in names[:10])}")
            if "任务编号" not in names:
                print("         注意：缺少「任务编号」列，任务同步会失败")
        except FeishuError as error:
            fail(str(error), "确认应用已被添加为该多维表格的协作者，且开通 bitable:app")

    print("\n自检结束。" + ("" if not failures else "存在需要修复的项。"))
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
