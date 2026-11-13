# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把整个程序打成一个「文件夹」。

为什么是文件夹（onedir）而不是单个 exe（onefile）：
onefile 每次双击都要把 700MB 内容解压到临时目录，启动要等半分钟以上，
朋友会以为程序坏了。文件夹模式只解压一次（安装时复制），双击几秒就开。

资源按相对路径打进去，打包后 app/*.py 里的 BASE_DIR 指向 _internal，
于是 vendor/ffmpeg、data/render-env、app/static 都能按原路径找到。
真实数据（数据库、上传视频、成片）由 launcher 通过 OVA_DATA_DIR 指到
exe 旁边的 data 目录，不跟程序一起打包。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(ROOT))

datas = [
    # 网页控制台（前端）
    (str(ROOT / "app" / "static"), "app/static"),
    # 渲染用字体、BGM 曲库、花字字体、颜色配置
    (str(ROOT / "data" / "render-env"), "data/render-env"),
    # 随包 ffmpeg / ffprobe
    (str(ROOT / "vendor" / "ffmpeg"), "vendor/ffmpeg"),
]

# 这些包除了 .py 还带配置文件 / 模型 / 词表，PyInstaller 默认不会自动收，
# 少一个就在运行时报「xxx.yaml does not exist」。OCR 的 config.yaml 和
# 识别模型就是这么丢的——出片跑到一半才炸，很难看出是打包问题。
for _package in (
    "onnxruntime",
    "edge_tts",
    "lark_oapi",
    "certifi",
):
    try:
        datas += collect_data_files(_package)
    except Exception:  # noqa: BLE001 - 包没装或没数据都不影响其它资源
        pass

# rapidocr 要整个目录连 .py 一起复制。
# 只收 data 文件会留下一个没有 __init__.py 的同名目录（ch_ppocr_v3_det 等），
# 导入时命中那个空壳，报 "has no attribute 'TextDetector'"。
_ocr_imports: list[str] = []
try:
    import rapidocr_onnxruntime as _rapidocr

    datas.append(
        (str(Path(_rapidocr.__file__).resolve().parent), "rapidocr_onnxruntime")
    )
    # 光复制文件不够：依赖分析没走过这棵树，它的 pyclipper / shapely
    # 就不会被收进来，运行时报 No module named 'pyclipper'。
    _ocr_imports = collect_submodules("rapidocr_onnxruntime") + [
        "pyclipper",
        "shapely",
        "yaml",
        "six",
    ]
except Exception:  # noqa: BLE001 - 没装就跳过，本机出片体检会提示
    pass

hiddenimports = _ocr_imports + collect_submodules("app") + [
    # uvicorn 按字符串动态加载这些实现，静态分析看不到
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # 发布模块在函数里才导入 playwright
    "playwright.sync_api",
]

a = Analysis(
    [str(ROOT / "tools" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 启动器已经不用 tkinter（改用 Windows 原生消息框），何况这台机器的
        # Python 根本没带它。留着只会让 PyInstaller 报一条让人心慌的警告。
        "tkinter",
        "_tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "notebook",
        "pytest",
        "_pytest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="房源视频助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 压完启动更慢，还容易被杀毒软件当成可疑文件，不值
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ROOT / "data" / "render-env" / "app.ico")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="房源视频助手",
)
