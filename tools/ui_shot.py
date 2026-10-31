"""把 WebUI 各个页面截成图，用来看改版后的实际效果。

用途有两个：
1. 改前端之后快速复查，不用每次手动点一遍；
2. 给别人看"现在长什么样"时，直接发图。

用的是系统自带的 Edge（channel="msedge"），不额外下载 Chromium。

用法：
    python tools/ui_shot.py
    python tools/ui_shot.py --base http://127.0.0.1:8770 --width 1440
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "ui-shots"

VIEWS = [
    ("dashboard", "工作台", None),
    ("review", "审核台", ".queue-card"),
    ("publish", "发布", ".platform-card"),
    ("properties", "房源库", "#property-rows tr"),
    ("tasks", "视频任务", "#task-rows tr"),
    ("settings", "设置", "#settings-form"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="截图 WebUI")
    parser.add_argument("--base", default="http://127.0.0.1:8770")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--channel", default="msedge", help="msedge / chrome / chromium")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                channel=args.channel,
                headless=True,
                # 本机 HTTP_PROXY 会把 127.0.0.1 也劫持走，必须显式禁用代理
                args=["--no-proxy-server"],
            )
        except Exception as error:  # noqa: BLE001
            print(f"启动浏览器失败（{args.channel}）：{error}")
            print("换一个：python tools/ui_shot.py --channel chrome")
            return 1

        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.goto(args.base, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1800)

        for key, label, selector in VIEWS:
            page.evaluate(f"showView('{key}')")
            # 这些页面是异步取数的（发布页还要探测两个平台，慢的时候 3~4 秒），
            # 光等固定时间会拍到空页面。
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=12000)
                except Exception:  # noqa: BLE001 - 空数据页面本来就没有这些元素
                    pass
            page.wait_for_timeout(1100)
            target = OUT_DIR / f"{key}.png"
            page.screenshot(path=str(target))
            print(f"  {label:6s} → {target}")

        # 单独来一张命令面板
        page.evaluate("openPalette()")
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT_DIR / "palette.png"))
        print(f"  命令面板 → {OUT_DIR / 'palette.png'}")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
