"""抖音上传页选择器校准工具。

`app/publish/douyin.py` 里的选择器是照经验写的，没有真实账号验证过。
抖音网页版改版很勤，第一次真机使用时大概率要对一遍。

这个脚本做的事：连上你的 Chrome，打开创作者中心上传页，
把候选选择器逐个试一遍并报告命中情况，同时截图。
按输出改 douyin.py 顶部的常量即可，比在代码里盲猜快得多。

前置：
    chrome.exe --user-data-dir=<项目>/data/chrome-profile --remote-debugging-port=9222
    然后在那个窗口里登录抖音创作者中心。

用法：
    python tools/douyin_probe.py
    python tools/douyin_probe.py --cdp http://127.0.0.1:9222 --headed
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "ui-shots"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

CANDIDATES = {
    "文件输入": ['input[type="file"]'],
    "标题框": [
        'input[placeholder*="填写作品标题"]',
        'input[placeholder*="标题"]',
        '.semi-input',
        'input[type="text"]',
    ],
    "描述框": [
        ".zone-container",
        'div[contenteditable="true"]',
        'textarea[placeholder*="添加作品简介"]',
        "textarea",
    ],
    "发布按钮": [
        'button:has-text("发布")',
        'button:has-text("立即发布")',
        ".content-confirm-container button",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音上传页选择器校准")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    args = parser.parse_args()

    # 先只用 HTTP 探一下端口，避免 playwright 抛出一长串难懂的异常
    try:
        with httpx.Client(trust_env=False, timeout=3.0) as client:
            version = client.get(f"{args.cdp}/json/version").json()
        print(f"已连接：{version.get('Browser', 'Chrome')}")
    except (httpx.HTTPError, ValueError) as error:
        print(f"连不上 {args.cdp}（{error.__class__.__name__}）")
        print(
            "\n先用专用 profile 启动 Chrome：\n"
            f'  chrome.exe --user-data-dir="{BASE_DIR / "data" / "chrome-profile"}" '
            "--remote-debugging-port=9222\n"
            "注意 Chrome 111+ 如果已有实例在跑，这个参数会被忽略，必须用独立的 user-data-dir。"
        )
        return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            print(f"\n当前地址：{page.url}")
            print(f"页面标题：{page.title()}")

            body = page.inner_text("body")[:600].replace("\n", " ")
            print(f"页面开头：{body[:240]}")

            login_markers = ["扫码登录", "验证码登录", "密码登录", "登录后查看"]
            if any(marker in body for marker in login_markers):
                print("\n⚠ 看起来没有登录。请在这个浏览器窗口里登录抖音创作者中心后重跑。")
                shot = OUT_DIR / "douyin-probe-login.png"
                page.screenshot(path=str(shot))
                print(f"截图：{shot}")
                return 2

            print("\n选择器命中情况：")
            for label, selectors in CANDIDATES.items():
                hits = []
                for selector in selectors:
                    try:
                        count = page.locator(selector).count()
                    except Exception:  # noqa: BLE001 - :has-text 在旧版本可能不支持
                        count = 0
                    if count:
                        hits.append(f"{selector} ×{count}")
                print(f"  {label:6s} " + ("　".join(hits) if hits else "（一个都没命中，需要手看页面改）"))

            shot = OUT_DIR / "douyin-probe-upload.png"
            page.screenshot(path=str(shot))
            print(f"\n截图：{shot}")
            print(
                "\n对照上面命中的选择器，改 app/publish/douyin.py 顶部的"
                "\nTITLE_SELECTORS / DESC_SELECTORS / PUBLISH_BUTTON_TEXTS 即可。"
            )
            return 0
        finally:
            page.close()


if __name__ == "__main__":
    raise SystemExit(main())
