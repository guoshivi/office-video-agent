# -*- coding: utf-8 -*-
"""截「视频库 → 某个模板详情」的图，用来核对拆解结果在界面上长什么样。

用法：
    python tools/ui_shot_template.py --id 0305b63c7ef9
"""
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "ui-shots"


def main() -> int:
    parser = argparse.ArgumentParser(description="截模板详情页")
    parser.add_argument("--base", default="http://127.0.0.1:8770")
    parser.add_argument("--id", required=True, help="模板 id")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1100)
    parser.add_argument("--channel", default="msedge")
    parser.add_argument("--name", default="template-detail")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=args.channel, headless=True, args=["--no-proxy-server"]
        )
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.goto(args.base, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        page.evaluate("showView('library')")
        page.wait_for_selector(".template-card", timeout=15000)
        page.evaluate(f"openTemplate('{args.id}')")
        page.wait_for_selector(".template-grid-wrap img", timeout=20000)
        page.wait_for_timeout(1500)
        target = OUT_DIR / f"{args.name}.png"
        page.screenshot(path=str(target), full_page=True)
        print("→", target)
        # 详情卡片自己再出一张：页面滚动区截不到下半部分（结构占比、下拉、镜头表）
        detail = page.query_selector(".template-detail")
        if detail is not None:
            body = OUT_DIR / f"{args.name}-detail.png"
            detail.screenshot(path=str(body))
            print("→", body)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
