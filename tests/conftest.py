"""测试用独立数据目录。

两条经验，都是踩出来的：

1. 不要用系统临时目录：Windows 的 %TEMP% 常被杀毒软件实时扫描，
   SQLite 会偶发 "attempt to write a readonly database"，导致测试随机失败。
2. 不要复用同一个固定目录：这台机器上的删除操作会走「安全删除」，
   shutil.rmtree 被拦下来又不报错，上一轮的库就原封不动留着——于是
   本轮跑出 UNIQUE 冲突、导入行数少一行这种和代码无关的假失败。

所以每次会话开一个带进程号的独立目录，天然干净，也不依赖删除成功。
"""

import atexit
import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / f".pytest-data-{os.getpid()}"
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["OVA_DATA_DIR"] = str(DATA_DIR)

# 退出时尽力清掉；删不掉也不影响下一次（下次换新目录）
atexit.register(lambda: shutil.rmtree(DATA_DIR, ignore_errors=True))

# 禁止测试真的去连工作台/飞书：清空外部地址，避免误触真实服务。
os.environ["WORKBENCH_URL"] = ""
os.environ["FEISHU_APP_ID"] = ""
os.environ["FEISHU_APP_SECRET"] = ""
