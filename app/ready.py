"""开工体检：把"能不能干活"翻成一句人话 + 一个按钮。

这套东西对懂电脑的人不复杂，对不懂的人很复杂——难的不是功能，是
「要开三个窗口、要懂四个词」：网页(8770)、小红书服务(18060)、专用浏览器(9222)，
加上 MCP / CDP / blocking / confirm_risky。

这个模块的职责就是把它们统一成三行字：

    出片        ✅ 准备好了
    发小红书    ⛔ 服务没启动        [帮我启动]
    发抖音      ⚠️ 浏览器没打开       [帮我打开]

原则：
1. **不出现端口号、不出现 MCP/CDP**。这些词对使用者没有任何信息量。
2. **每一项都能一键修**。只说"没准备好"而让人自己去开窗口，等于没说。
3. **失败要说下一步做什么**，不是"连接失败(ConnectError)"。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .db import DATA_DIR, get_settings, transaction

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
XHS_EXE = BASE_DIR / "vendor" / "xhs-mcp" / "xiaohongshu-mcp.exe"
CHROME_PROFILE = DATA_DIR / "chrome-profile"
DOUYIN_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

# 本机出片这条链上真正会缺的东西。少任何一个，出片都会以
# 「子进程退出码 1：No module named xxx」这种话结束——对使用者等于没说。
RENDER_MODULES = [
    ("edge_tts", "口播配音"),
    ("numpy", "选镜计算"),
    ("cv2", "画面检测"),
    ("PIL", "花字贴片"),
]
RENDER_FONT = BASE_DIR / "data" / "render-env" / "NotoSansSCBlack.ttf"
RENDER_PACKAGES = [
    "edge-tts>=6.1,<8",
    "numpy>=1.26,<3",
    "opencv-python-headless>=4.9,<5",
    "pillow>=10,<12",
]

XHS_PORT = 18060
CDP_PORT = 9222

# 体检结果缓存：首页每次切回来都重查会等两三秒，20 秒内的结论足够准
CACHE_SECONDS = 20
_CACHE: dict = {"at": 0.0, "data": None}

# 别在 Windows 上弹黑框：DETACHED_PROCESS 让子进程完全脱离
_DETACHED = 0
if sys.platform == "win32":  # pragma: no cover - 平台相关
    _DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


@dataclass
class Item:
    key: str
    label: str
    ok: bool
    detail: str
    action: str = ""
    action_label: str = ""
    # ok=False 但 optional=True 的项不算"卡住"，只是建议做
    optional: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _wait_port(port: int, seconds: float = 20.0) -> bool:
    """等服务起来。刚启动的 exe 需要几秒，这段时间按钮上是"正在启动…"。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.5)
    return False


def _find_chrome() -> str:
    """找本机 Chrome。找不到返回空串，由界面提示"装个 Chrome 就行"。"""
    candidates = [
        os.getenv("CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


# ------------------------------------------------------------------ 体检

def _render_item(settings: dict) -> Item:
    """出片这一项：默认查本机，只有在明确配了工作台的时候才去连它。

    默认值改成 local 是刻意的。整套东西是要整包拷给别人电脑用的，
    对面既没有我们的工作台，也不该为了出片先去架一台机器。
    """
    engine = (settings.get("render_engine") or "local").strip().lower()
    if engine == "workbench":
        url = (settings.get("workbench_url") or "").strip()
        if url:
            return _workbench_item(url)
    return _local_render_item()


def _local_render_item() -> Item:
    missing = [
        label for module, label in RENDER_MODULES
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        return Item(
            "render", "出片", False,
            f"还缺出片组件：{'、'.join(missing)}。装一次就好，之后不用管",
            action="install_render_deps", action_label="一键装好",
        )

    from .shots import ShotError, _find_binary

    try:
        _find_binary("ffmpeg")
        _find_binary("ffprobe")
    except ShotError:
        return Item(
            "render", "出片", False,
            "找不到 ffmpeg。项目里的 vendor/ffmpeg 是随包带的，可能被清理掉了",
        )
    if not RENDER_FONT.is_file():
        return Item(
            "render", "出片", False,
            "缺渲染字体，字幕会变成方块。data/render-env 里的字体别删",
        )
    # 配音来源顺带说一句：配了豆包就说豆包，没配就说清是免费方案
    from . import tts

    tts_state = tts.status()
    if tts_state.get("provider") == "volcano" and tts_state.get("ready"):
        detail = "本机出片就绪，配音用豆包音色"
    elif tts_state.get("provider") == "volcano":
        detail = "本机出片就绪；豆包语音还差音色 ID，先去设置里补"
    else:
        detail = "本机出片就绪；配音用免费方案，想更真就去设置里填豆包凭据"
    return Item("render", "出片", True, detail)


def _workbench_item(url: str) -> Item:
    try:
        # 这是局域网地址，2 秒连不上就是真连不上，别让首页转圈。
        # 路径要和 workbench_client 一致（/api/worker），否则正常的机器也会 404。
        response = httpx.get(f"{url.rstrip('/')}/api/worker", timeout=2.0, trust_env=False)
        if response.status_code < 400:
            return Item("render", "出片", True, "视频工作台在线")
        return Item(
            "render", "出片", False,
            f"视频工作台有响应但状态不对（{response.status_code}），可能正在重启",
            action="open_settings", action_label="去看设置",
        )
    except Exception:  # noqa: BLE001 - 连不上的原因对使用者没意义
        return Item(
            "render", "出片", False,
            "连不上视频工作台，确认那台机器开着、和本机在同一个网络",
            action="open_settings", action_label="去看设置",
        )


def _xhs_item(settings: dict, status: dict) -> Item:
    enabled = settings.get("publish_xhs_enabled") == "1"
    if not enabled:
        return Item(
            "xiaohongshu", "发小红书", False,
            "还没打开，打开后审核通过的片子可以自动发",
            action="enable_xiaohongshu", action_label="打开小红书发布",
            optional=True,
        )
    if not XHS_EXE.is_file():
        return Item(
            "xiaohongshu", "发小红书", False,
            "还缺发布服务文件，跑一次 tools/fetch_publish_tools.py 就能下载",
            action="", action_label="",
        )
    if not status.get("reachable"):
        return Item(
            "xiaohongshu", "发小红书", False,
            "发布服务没在跑",
            action="start_xiaohongshu", action_label="帮我启动",
        )
    if not status.get("logged_in"):
        return Item(
            "xiaohongshu", "发小红书", False,
            "服务在跑，但还没登录小红书",
            action="login_xiaohongshu", action_label="扫码登录",
        )
    return Item("xiaohongshu", "发小红书", True, "已登录，可以发")


def _douyin_item(settings: dict, status: dict) -> Item:
    enabled = settings.get("publish_douyin_enabled") == "1"
    if not enabled:
        return Item(
            "douyin", "发抖音", False,
            "还没打开，打开后审核通过的片子可以自动发",
            action="enable_douyin", action_label="打开抖音发布",
            optional=True,
        )
    if not _find_chrome():
        return Item(
            "douyin", "发抖音", False,
            "这台电脑上没找到 Chrome，装一个就好",
            action="", action_label="",
        )
    if not status.get("reachable"):
        return Item(
            "douyin", "发抖音", False,
            "发布用的浏览器窗口没打开",
            action="start_douyin", action_label="帮我打开",
        )
    if not status.get("logged_in"):
        return Item(
            "douyin", "发抖音", False,
            "浏览器开着，但还没登录抖音",
            action="start_douyin", action_label="打开登录页",
        )
    return Item("douyin", "发抖音", True, "已登录，可以发")


def _platform_probe(settings: dict) -> tuple[dict, dict]:
    """一次拿两个平台的状态。失败也不能让首页挂掉。"""
    from .publish import platform_status

    try:
        platforms = platform_status(settings)
        return platforms.get("xiaohongshu", {}), platforms.get("douyin", {})
    except Exception as error:  # noqa: BLE001 - 体检本身不能把首页搞挂
        logger.warning("平台体检失败：%s", error)
        return {}, {}


def check(force: bool = False) -> dict:
    """三项体检。这个函数是首页第一屏，宁可给 20 秒前的结论也不能让它转圈。

    三项各自会走网络（工作台 / 小红书服务 / 浏览器调试端口），
    并发之后仍然要两三秒。所以结果缓存一小会儿，只有点了按钮或
    `?refresh=1` 才强制重查——否则每次切回首页都要等一轮。
    """
    now = time.monotonic()
    if not force and _CACHE["data"] is not None and now - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["data"]

    settings = get_settings()
    with ThreadPoolExecutor(max_workers=2) as pool:
        render_future = pool.submit(_render_item, settings)
        platform_future = pool.submit(_platform_probe, settings)
        render = render_future.result()
        xhs_status, douyin_status = platform_future.result()

    items = [
        render,
        _xhs_item(settings, xhs_status),
        _douyin_item(settings, douyin_status),
    ]
    blocking = [item for item in items if not item.ok and not item.optional]
    optional = [item for item in items if not item.ok and item.optional]

    if not items[0].ok:
        headline = "还不能开工：先把出片引擎连上"
    elif blocking:
        headline = f"出片可以了，发布还差 {len(blocking)} 件事"
    elif optional:
        headline = "可以出片了。打开发布的话，审完就能自动发出去"
    else:
        headline = "全部就绪，可以开工"

    result = {
        # ready 是"全绿"，can_render 是"能不能出片"——两者刻意分开：
        # 发布没配好不该让人以为连片子都做不了
        "ready": not blocking,
        "can_render": items[0].ok,
        "headline": headline,
        "items": [item.as_dict() for item in items],
        "blocking": len(blocking),
        "optional": len(optional),
        "checked_at": time.strftime("%H:%M:%S"),
    }
    _CACHE["at"] = now
    _CACHE["data"] = result
    return result


# ------------------------------------------------------------------ 一键修

def install_render_deps() -> dict:
    """一键装齐出片组件。

    这是「No module named 'edge_tts'」唯一体面的解法：让人去开命令行、
    进虚拟环境、敲 pip，对不懂电脑的人来说等于让他放弃。
    """
    if getattr(sys, "frozen", False):
        # 打包成 exe 之后没有可用的 pip，只能换一份完整的包
        return {
            "ok": False,
            "message": "这个版本是打包过的，缺组件要换成完整包，找个懂的人重新发一份就行",
        }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", *RENDER_PACKAGES],
            capture_output=True, text=True, timeout=900, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "装得太久没装完，网络可能不太行，过一会儿再点一次"}
    except OSError as error:
        return {"ok": False, "message": f"装不上：{error}"}

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1][:160] if detail else "未知原因"
        return {"ok": False, "message": f"装失败了（{tail}），检查一下网络再点一次"}

    still_missing = [
        label for module, label in RENDER_MODULES
        if importlib.util.find_spec(module) is None
    ]
    _CACHE["at"] = 0.0   # 装完立刻重查，别让首页还挂着 20 秒前的红条
    if still_missing:
        return {"ok": False, "message": f"装完了但还差 {'、'.join(still_missing)}，重启一下程序再看"}
    return {"ok": True, "message": "出片组件装好了，现在可以出片"}


def start_xiaohongshu() -> dict:
    """把小红书发布服务拉起来。已经在跑就什么都不做。"""
    if _port_open(XHS_PORT):
        return {"ok": True, "message": "发布服务本来就在跑"}
    if not XHS_EXE.is_file():
        return {
            "ok": False,
            "message": "还缺发布服务文件。在项目目录跑一次 tools/fetch_publish_tools.py 就能下载。",
        }
    try:
        subprocess.Popen(
            [str(XHS_EXE), "-port", f":{XHS_PORT}"],
            cwd=str(XHS_EXE.parent),
            creationflags=_DETACHED,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as error:
        return {"ok": False, "message": f"启动失败：{error}"}

    if _wait_port(XHS_PORT):
        return {"ok": True, "message": "发布服务已启动"}
    return {"ok": False, "message": "等了一会儿还没起来，可能是杀毒软件拦了，或端口被占用"}


def start_douyin() -> dict:
    """打开专用浏览器窗口。这个窗口就是"发布用的那个浏览器"，别手关。"""
    if _port_open(CDP_PORT):
        return {"ok": True, "message": "发布用的浏览器已经开着"}
    chrome = _find_chrome()
    if not chrome:
        return {"ok": False, "message": "这台电脑上没找到 Chrome，装一个就好"}

    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            [
                chrome,
                f"--user-data-dir={CHROME_PROFILE}",
                f"--remote-debugging-port={CDP_PORT}",
                "--no-first-run",
                "--no-default-browser-check",
                DOUYIN_UPLOAD_URL,
            ],
            creationflags=_DETACHED,
            close_fds=True,
        )
    except OSError as error:
        return {"ok": False, "message": f"启动失败：{error}"}

    if _wait_port(CDP_PORT, seconds=25):
        return {"ok": True, "message": "浏览器已打开，请在里面登录抖音，然后点「检查一下」"}
    return {
        "ok": False,
        "message": "浏览器开了但没连上。如果本来就有 Chrome 窗口开着，把那些窗口都关掉再试一次。",
    }


def toggle_platform(platform: str, enabled: bool) -> dict:
    key = {"xiaohongshu": "publish_xhs_enabled", "douyin": "publish_douyin_enabled"}.get(platform)
    if key is None:
        return {"ok": False, "message": "未知平台"}
    with transaction() as connection:
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "1" if enabled else "0"),
        )
    label = "小红书" if platform == "xiaohongshu" else "抖音"
    return {"ok": True, "message": f"已{'打开' if enabled else '关闭'}{label}发布"}


def autostart() -> dict:
    """程序启动时把已启用的发布服务顺手拉起来。

    不懂电脑的人不会记得"先双击那个黑窗口"。既然他已经把发布开关打开，
    就当他明天也要发——开机先起好，省掉一次求助。
    没打开的平台一律不碰，免得平白弹一个浏览器窗口把人吓到。
    """
    settings = get_settings()
    started: dict = {}
    try:
        if settings.get("publish_xhs_enabled") == "1":
            started["xiaohongshu"] = start_xiaohongshu()
        if settings.get("publish_douyin_enabled") == "1":
            started["douyin"] = start_douyin()
    except Exception as error:  # noqa: BLE001 - 起不来不影响网页本身能用
        logger.warning("自动启动发布服务失败：%s", error)
    return started


# ------------------------------------------------------------------ 诊断

def diagnose() -> dict:
    """生成一段可以直接微信发出去的诊断文本。

    不懂电脑的人描述不清现象（"它不动了"），懂的人问不出关键信息。
    与其来回三趟，不如让他复制这一段。
    """
    from . import __version__
    from .publish import platform_status

    settings = get_settings()
    lines = [f"房源视频助手 v{__version__}", f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"]

    try:
        report = check()
        lines.append("")
        lines.append("【开工体检】")
        lines.append(f"结论：{report['headline']}")
        for item in report["items"]:
            mark = "OK" if item["ok"] else ("未启用" if item["optional"] else "有问题")
            lines.append(f"- {item['label']}：{mark} · {item['detail']}")
    except Exception as error:  # noqa: BLE001
        lines.append(f"体检本身失败：{error}")

    try:
        platforms = platform_status(settings)
        lines.append("")
        lines.append("【发布平台】")
        for name, status in platforms.items():
            label = "小红书" if name == "xiaohongshu" else "抖音"
            lines.append(
                f"- {label}：启用={settings.get('publish_xhs_enabled') if name == 'xiaohongshu' else settings.get('publish_douyin_enabled')}"
                f" 连得上={status.get('reachable')} 已登录={status.get('logged_in')}"
            )
    except Exception as error:  # noqa: BLE001
        lines.append(f"平台状态读取失败：{error}")

    try:
        from .review import store

        counts = store.counts()
        lines.append("")
        lines.append(
            "【队列】"
            f"待审 {counts.get('pending', 0)} / 通过 {counts.get('approved', 0)} "
            f"/ 驳回 {counts.get('rejected', 0)} / 已发布 {counts.get('published', 0)}"
        )
    except Exception as error:  # noqa: BLE001
        lines.append(f"队列读取失败：{error}")

    logs = _tail_log()
    if logs:
        lines.append("")
        lines.append("【最近的报错】")
        lines.extend(logs)

    return {"text": "\n".join(lines)}


def _tail_log(limit: int = 12) -> list[str]:
    """抓 server.log 里最近的报错行。没有就返回空。"""
    path = DATA_DIR / "server.log"
    if not path.is_file():
        return []
    try:
        tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return []
    bad = [line.strip() for line in tail if ("ERROR" in line or "Traceback" in line or "错误" in line)]
    return bad[-limit:]
