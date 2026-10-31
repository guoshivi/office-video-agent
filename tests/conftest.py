"""测试用固定数据目录。

不要用系统临时目录：Windows 的 %TEMP% 常被杀毒软件实时扫描，
SQLite 会偶发 "attempt to write a readonly database"，导致测试随机失败。
"""

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / ".pytest-data"

if DATA_DIR.exists():
    shutil.rmtree(DATA_DIR, ignore_errors=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["OVA_DATA_DIR"] = str(DATA_DIR)

# 禁止测试真的去连工作台/飞书：清空外部地址，避免误触真实服务。
os.environ["WORKBENCH_URL"] = ""
os.environ["FEISHU_APP_ID"] = ""
os.environ["FEISHU_APP_SECRET"] = ""
