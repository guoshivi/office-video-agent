"""本地出片的子进程入口：python -m app.render_cli <task_id>

为什么要单开进程：渲染链路会同时加载 onnxruntime（OCR）、OpenCV 和 torch，
Windows 上它们的 OpenMP 运行时偶尔互相踩，直接在 uvicorn 线程里跑会
把整个服务带崩（进程消失、日志无 traceback）。子进程崩了只影响这一条片子，
服务照常活着，失败原因也能从返回码判断。

stdout 只输出一行 JSON，方便调用方解析：
    {"ok": true, "output": "D:/ova/data/exports/xxx/local.mp4"}
    {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"ok": False, "error": "缺少任务 ID"}, ensure_ascii=False))
        return 2
    task_id = argv[1]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        from app.db import connect
        from app.style_render import render_task_locally

        row = connect().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            print(json.dumps({"ok": False, "error": f"任务不存在：{task_id}"}, ensure_ascii=False))
            return 3
        output = render_task_locally(row)
        print(json.dumps({"ok": True, "output": str(output)}, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - 子进程必须把失败原因带回去
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(error)[:800]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
