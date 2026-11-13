"""起一个用完即弃的实例：数据在临时目录，绝不碰真实房源和成片。

为什么需要它：改完出片链路，总得真跑一遍才敢说"通了"。但在真实库上跑，
用户的工作台里就多出一条"用测试素材出的片"，绑在真实房号上——看着像系统
自己在乱发片。这个脚本把 OVA_DATA_DIR 指到独立临时目录 + 独立端口，
跑完连目录一起删，真实 data/office_video_agent.db 一个字节都不动。

用法：
    python tools/dev_sandbox.py                 # 8771 端口，退出即删临时库
    python tools/dev_sandbox.py --port 8780
    python tools/dev_sandbox.py --data D:/tmp/ova-verify --keep   # 留库方便看结果
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="起一个隔离数据目录的验证实例")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--data", default="", help="指定数据目录；不填则用临时目录")
    parser.add_argument("--keep", action="store_true", help="退出后保留数据目录")
    args = parser.parse_args()

    data_dir = Path(args.data) if args.data else Path(tempfile.mkdtemp(prefix="ova-sandbox-"))
    # 直接运行 tools/ 下的脚本时，sys.path[0] 是 tools/，import app 会找不到
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    # 必须在 import app.* 之前设好，db 模块在导入时就把路径定下来了
    os.environ["OVA_DATA_DIR"] = str(data_dir)
    os.environ["WORKBENCH_URL"] = ""  # 验证环境不连真实工作台

    import uvicorn

    print(f"沙盒数据目录：{data_dir}")
    print(f"打开 http://127.0.0.1:{args.port}  （这里的房源、任务都是假的，随便造）")
    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        if args.keep:
            print(f"已保留：{data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)
            print("沙盒已清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
