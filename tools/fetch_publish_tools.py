"""拉取发布用的第三方二进制（小红书 MCP 服务 + 登录工具）。

这些东西每个 10~15MB，不适合塞进 GitHub 仓库，
所以仓库里只留这个脚本，朋友电脑上跑一次就装好。

用法：
    python tools/fetch_publish_tools.py
    python tools/fetch_publish_tools.py --force     # 忽略已有文件重新下载
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
TARGET_DIR = BASE_DIR / "vendor" / "xhs-mcp"

XHS_VERSION = "v2.5.5"
XHS_BASE = f"https://github.com/xpzouying/xiaohongshu-mcp/releases/download/{XHS_VERSION}"

ASSETS = {
    "xiaohongshu-mcp.exe": f"{XHS_BASE}/xiaohongshu-mcp-windows-amd64.exe",
    "xiaohongshu-login.exe": f"{XHS_BASE}/xiaohongshu-login-windows-amd64.exe",
}

# 只做体量兜底：下载到 404 页面或半截文件时能看出来，不追求严格校验
MIN_BYTES = 5 * 1024 * 1024


def download(url: str, target: Path) -> None:
    print(f"  下载 {url}")
    with urlopen(url, timeout=300) as response:  # noqa: S310 - 固定的 GitHub 发布地址
        payload = response.read()
    if len(payload) < MIN_BYTES:
        raise RuntimeError(
            f"下载到的只有 {len(payload)} 字节，可能是 404 或网络被拦截：{url}"
        )
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()[:12]
    print(f"  完成 {target.name}  {len(payload) / 1048576:.1f}MB  sha256:{digest}…")


def main() -> int:
    parser = argparse.ArgumentParser(description="拉取小红书发布服务")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    args = parser.parse_args()

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    print(f"目标目录：{TARGET_DIR}")
    print(f"版本：{XHS_VERSION}")

    failed = False
    for name, url in ASSETS.items():
        target = TARGET_DIR / name
        if target.exists() and not args.force:
            print(f"  跳过 {name}（已存在，{target.stat().st_size / 1048576:.1f}MB）")
            continue
        try:
            download(url, target)
        except (URLError, OSError, RuntimeError) as error:
            print(f"  失败 {name}：{error}", file=sys.stderr)
            failed = True

    if failed:
        print(
            "\n有文件没下下来。如果网络访问 GitHub 受限，"
            "可以手动从 releases 页面下载后放进 vendor/xhs-mcp/。",
            file=sys.stderr,
        )
        return 1

    print(
        "\n下一步：\n"
        "  1. 启动发布服务  vendor\\xhs-mcp\\xiaohongshu-mcp.exe -port :18060\n"
        "  2. 在 WebUI 点「扫码登录」，或用命令行取二维码\n"
        "  3. 注意：小红书同一账号不能多端网页同时登录，登了 MCP 就别再开浏览器版"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
