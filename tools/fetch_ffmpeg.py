"""把 ffmpeg / ffprobe 取到 vendor/ffmpeg 里。

为什么需要这个脚本：出片链离不开 ffmpeg，而且必须是**带 libass 和 libx264** 的
那一类（字幕靠 libass 烧，现在字幕是 ASS）。靠用户自己去 winget 装一个，
版本和编译选项都不一定对；靠开发机上的试验包目录，换台机器就没了。

所以规矩是：ffmpeg 跟着项目走，放在 vendor/ffmpeg/。
二进制不进 git（200MB，仓库受不了），新机器跑一次这个脚本就行。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TARGET = BASE / "vendor" / "ffmpeg"
URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
WANTED = ("ffmpeg.exe", "ffprobe.exe")
NEEDLES = ("--enable-libass", "--enable-libx264", "--enable-libfreetype")


def _already_ok() -> bool:
    ffmpeg = TARGET / "ffmpeg.exe"
    if not ffmpeg.is_file() or not (TARGET / "ffprobe.exe").is_file():
        return False
    try:
        out = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return all(needle in out for needle in NEEDLES)


def main() -> int:
    if _already_ok():
        print(f"已经就位：{TARGET}")
        return 0

    TARGET.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ffmpeg-dl-") as work:
        archive = Path(work) / "ffmpeg.zip"
        print(f"下载 {URL}")
        with urllib.request.urlopen(URL, timeout=120) as response, archive.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        print(f"下载完成：{archive.stat().st_size / 1048576:.0f} MB")

        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                name = Path(member.filename).name
                if name in WANTED:
                    with bundle.open(member) as source, (TARGET / name).open("wb") as target:
                        shutil.copyfileobj(source, target)
                    print(f"  解出 {name}")

    if not _already_ok():
        print("装好了但版本不对：缺 libass 或 libx264，出片会失败。换一个 ffmpeg 构建再试。")
        return 1
    print(f"完成：{TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
