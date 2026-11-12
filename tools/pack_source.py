"""把当前工作目录打成「可直接在另一台电脑继续开发」的源码包。

刻意不打的：
- .venv / models / data 下的业务数据 —— 几百 MB 到几 GB，另一台机器重建更快
- vendor 里的发布工具 exe 和 cookies.json —— 第三方二进制 + 登录凭据，绝不能外带
- .env —— 里面有 key 和本机路径

保留 data/render-env（字体 + fontconfig），因为渲染字幕要用，而且字体是从 OTF
转出来的，另一台机器没有原始 OTF 就补不回来。
"""

from __future__ import annotations

import fnmatch
import zipfile
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ARCHIVE = BASE / f"房源视频助手-源码-{date.today():%Y%m%d}.zip"

# 整个目录跳过
SKIP_DIRS = {
    ".venv", ".git", ".pytest_cache", ".pytest-data", ".workbuddy", ".idea", ".vscode",
    "__pycache__", "node_modules", "build", "dist", "models", "vendor",
    "data",
}
# data 里只带渲染环境（字体、fontconfig），其他都是本机业务数据和缓存
DATA_KEEP = {
    "render-env/NotoSansSCBlack.ttf",
    "render-env/fonts.conf",
    "render-env/app.ico",
}
SKIP_FILES = [
    "*.pyc", "*.pyo", "*.log", "*.db", "*.db-journal", "*.sqlite", "*.sqlite3",
    ".env", "*.spec.bak", "*.mp4", "*.mov", "*.wav", "*.mp3", "*.zip",
    ".DS_Store", "Thumbs.db",
]

SETUP = """# 另一台电脑上怎么跑起来

这个包是**源码 + 渲染字体**，不含虚拟环境、模型权重、房源数据和 API Key。

## 1. 环境

- Windows 10/11，**Python 3.12**（3.13 有些依赖轮子还没有）
- **FFmpeg**：`winget install Gyan.FFmpeg`，装完新开一个终端让 PATH 生效

## 2. 装依赖

```
cd <解压目录>
python -m venv .venv
.venv\\Scripts\\python.exe -m pip install -U pip
.venv\\Scripts\\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

要用到「本地语义检索 / 参考视频拆解」再加装：

```
.venv\\Scripts\\python.exe -m pip install -r requirements-embed.txt
```

注意：`requirements-embed.txt` 里的 torch 是 CUDA 版（cu130），PyPI 上没有，
按文件里的注释从镜像装对应 wheel；没有显卡就用 PyPI 的 CPU 版 torch，
代码会自动退回 CPU（慢一些但能跑）。

## 3. 下模型（按需）

```
# 语音转写（参考视频拆解用），约 1.5GB
.venv\\Scripts\\python.exe tools\\fetch_asr_model.py

# 语义检索用的 embeddinggemma-2，约 1.5GB，从 ModelScope 下到 models\\embeddinggemma-2
# 具体命令见 requirements-embed.txt 注释
```

不下模型也能用主流程（上传 → 文案 → 出片），只是「参考视频模仿」和「语义搜索」不可用。

## 4. 配置

```
copy .env.example .env
```

按需填：`OVA_PORT`（默认 8770）、`WORKBENCH_URL`、`ARK_API_KEY`（豆包）。
**AI 文案的 key 建议直接在网页「系统设置 → AI 文案」里填**，那样存在数据库里、
改完即时生效，不用重启。

## 5. 启动

```
start.bat
```

或者直接用虚拟环境跑：

```
.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8770
```

首次启动会自动建库（`data/office_video_agent.db`），空库，没有房源数据。
房源从网页「导入 Excel」进来，表头必须落在**第 2 行**（第 1 行是标题）。

## 6. 桌面启动器（可选）

`tools/launcher.py` 是桌面 exe 的源码，另一台机器上重新打包：

```
.venv\\Scripts\\python.exe -m pip install pyinstaller
.venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "房源视频助手" --icon data\\render-env\\app.ico tools\\launcher.py
```

打包出来的 exe 里写死了项目路径 `D:\\ova`，换路径时设环境变量 `OVA_HOME` 指到新目录即可。

## 7. 自测

```
.venv\\Scripts\\python.exe -m pytest tests\\ -q
```

## 包里没有的东西

- 房源数据库、上传的视频、成片（都在 `data/` 下，属业务数据）
- 大模型权重（`models/`）
- 小红书/抖音发布工具的 exe（`vendor/`，用 `tools/fetch_publish_tools.py` 重新取）
- `.env` 和任何 API Key
"""


def _skip(relative: Path) -> bool:
    parts = relative.parts
    # data 要单独判：只有渲染环境那几个文件带，其它业务数据不带
    if parts[0] == "data":
        return "/".join(parts[1:]) not in DATA_KEEP
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return True
    name = relative.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in SKIP_FILES)


def main() -> None:
    files: list[Path] = []
    for path in BASE.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(BASE)
        if _skip(relative):
            continue
        files.append(relative)

    files.sort(key=lambda item: item.as_posix())
    with zipfile.ZipFile(ARCHIVE, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            archive.write(BASE / relative, relative.as_posix())
        archive.writestr("部署说明.md", SETUP)

    size_mb = ARCHIVE.stat().st_size / 1048576
    print(f"打包完成：{ARCHIVE.name}（{size_mb:.1f} MB，{len(files)} 个文件 + 部署说明.md）")
    top: dict[str, int] = {}
    for relative in files:
        key = relative.parts[0] if len(relative.parts) > 1 else "（根目录）"
        top[key] = top.get(key, 0) + 1
    for key, count in sorted(top.items(), key=lambda item: -item[1]):
        print(f"  {key}: {count}")


if __name__ == "__main__":
    main()
