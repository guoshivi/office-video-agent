"""从魔搭下载 faster-whisper 权重（HuggingFace 在国内经常只有几十 KB/s）。

用法：
    .venv\\Scripts\\python.exe tools\\fetch_asr_model.py
    .venv\\Scripts\\python.exe tools\\fetch_asr_model.py --repo pengzhendong/faster-whisper-large-v3-turbo

下载到 models/faster-whisper-large-v3-turbo/，共约 1.5GB。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "pengzhendong/faster-whisper-large-v3-turbo"
FILES = ["config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json", "model.bin"]


def download(repo: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    root = f"https://modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath="
    for name in FILES:
        destination = target / name
        expected = 0
        if destination.is_file():
            expected = destination.stat().st_size
        request = Request(f"{root}{name}")
        if expected:
            request.add_header("Range", f"bytes={expected}-")
        try:
            response = urlopen(request, timeout=120)  # noqa: S310 - 固定域名
        except HTTPError as error:
            if error.code == 416:      # 断点已经在文件末尾：说明上次已经下完
                print(f"  {name:<28}{expected / 1048576:>8.1f} MB（已完整）", flush=True)
                continue
            raise
        with response:
            mode = "ab" if response.status == 206 else "wb"
            with destination.open(mode) as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
        size = destination.stat().st_size / 1048576
        print(f"  {name:<28}{size:>8.1f} MB", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--dir", default="")
    args = parser.parse_args()

    target = Path(args.dir) if args.dir else BASE_DIR / "models" / "faster-whisper-large-v3-turbo"
    print(f"从魔搭下载 {args.repo} → {target}")
    try:
        download(args.repo, target)
    except Exception as error:  # noqa: BLE001
        print(f"下载失败：{error}", file=sys.stderr)
        return 1
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
