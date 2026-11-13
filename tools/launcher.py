"""桌面启动器：双击即启动房源视频助手并打开工作台。

两种运行方式，同一份代码：

- **源码方式**（开发时）：找项目里的 .venv/Scripts/python.exe 起 uvicorn
- **打包方式**（交付给朋友）：用 exe 自己带 --serve 起服务，不依赖任何外部 Python

数据一律写在 exe 旁边的 data 目录，不放 C 盘用户目录，也不放 PyInstaller 的
临时解压目录——后者每次运行都换路径，数据库放那儿等于每次都从零开始。

界面上刻意不用 tkinter：打包环境里这台机器的 Python 根本没带 tkinter，
而朋友的电脑上更不会有。需要弹窗时直接调 Windows 自带的 MessageBox，
需要进度时开一个本地网页——两样都是零依赖，也更容易做得好着。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "房源视频助手"
HOST = "0.0.0.0"


def _port() -> int:
    """服务端口。默认 8770，被占用时可以用环境变量 OVA_PORT 换一个。

    start.bat 里也认同一个变量，两边保持一致，免得源码方式和 exe 方式行为不同。
    """
    try:
        value = int(os.environ.get("OVA_PORT", "") or PORT_DEFAULT)
    except ValueError:
        value = PORT_DEFAULT
    return value if 1024 <= value <= 65535 else PORT_DEFAULT


PORT_DEFAULT = 8770
PORT = _port()
LOCAL_URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = f"{LOCAL_URL}/api/health"
START_TIMEOUT = 90.0

FROZEN = bool(getattr(sys, "frozen", False))

# Windows 消息框常量
MB_YESNO = 0x04
MB_ICONQUESTION = 0x20
MB_ICONINFO = 0x40
MB_ICONERROR = 0x10
IDYES = 6

STARTING_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>房源视频助手 · 正在启动</title>
<style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0e1117;color:#e8ecf4;
font-family:"Microsoft YaHei UI","Microsoft YaHei",system-ui,sans-serif}
.card{width:340px;text-align:center}
h1{font-size:17px;font-weight:600;margin:0 0 10px;letter-spacing:.02em}
p{font-size:13px;color:#8b95a9;margin:0 0 24px;line-height:1.7;min-height:44px}
.track{height:3px;background:#232936;border-radius:2px;overflow:hidden}
.bar{height:100%;width:6%;border-radius:2px;transition:width .45s ease;
background:linear-gradient(90deg,#4c8dff,#8ab4ff)}
.fail{color:#ff8080}
</style></head>
<body><div class="card">
<h1>房源视频助手</h1>
<p id="tip">正在启动，稍等一下…</p>
<div class="track"><div class="bar" id="bar"></div></div>
</div>
<script>
const next = new URLSearchParams(location.search).get('next') || 'http://127.0.0.1:8770';
const bar = document.getElementById('bar');
const tip = document.getElementById('tip');
let tries = 0;
(function tick(){
  tries++;
  fetch(next + '/api/health', {cache: 'no-store'})
    .then(r => {
      if (!r.ok) throw new Error('not ready');
      tip.textContent = '好了，正在进入工作台…';
      bar.style.width = '100%';
      setTimeout(() => location.replace(next), 250);
    })
    .catch(() => {
      if (tries > 150) {
        tip.className = 'fail';
        tip.textContent = '启动失败：超过 75 秒还没起来。请重新双击程序，或把 data/tmp/uvicorn.log 发给维护的人。';
        return;
      }
      bar.style.width = Math.min(92, 6 + tries * 1.5) + '%';
      setTimeout(tick, 500);
    });
})();
</script></body></html>
"""


def install_dir() -> Path:
    """程序安装目录：打包后是 exe 所在文件夹，源码方式下是项目根。"""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    override = os.environ.get("OVA_HOME", "").strip()
    if override and Path(override).is_dir():
        return Path(override)
    return Path(__file__).resolve().parent.parent


BASE = install_dir()
# 可写数据目录：exe 旁边的 data。打包后必须显式指定，否则 app/db.py 会把
# 数据库建到 PyInstaller 的临时目录里，关掉程序就全丢。
DATA = Path(os.environ.get("OVA_DATA_DIR") or (BASE / "data"))
LOG_DIR = DATA / "tmp"
LOG_FILE = LOG_DIR / "uvicorn.log"
VENV_PY = BASE / ".venv" / "Scripts" / "python.exe"


def _say(text: str, style: int = MB_ICONINFO) -> int:
    """弹一个 Windows 原生消息框，返回点了哪个按钮。

    不用 tkinter 是因为打包环境（以及朋友的电脑）未必带这个模块。
    """
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, style))
    except Exception:  # noqa: BLE001 - 弹不出来也不能把启动流程带崩
        return 0


# ------------------------------------------------------------------ 健康检查

def _healthy(timeout: float = 2.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(HEALTH_URL, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _port_busy() -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def _pid_on_port() -> int | None:
    """从 netstat 里翻出占着 8770 的进程号（不引入额外依赖）。"""
    try:
        output = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and "LISTEN" in parts[3].upper():
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


# ------------------------------------------------------------------ 启动 / 停止

def _server_env() -> dict:
    env = os.environ.copy()
    env["OVA_DATA_DIR"] = str(DATA)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def server_command() -> list[str] | None:
    """起服务要执行的命令。打包后是「自己 + --serve」，源码方式走 venv。"""
    if FROZEN:
        return [sys.executable, "--serve"]
    if VENV_PY.is_file():
        return [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)]
    return None


def start_server() -> tuple[bool, str]:
    command = server_command()
    if command is None:
        return False, f"找不到 Python 环境：{VENV_PY}\n\n源码方式运行需要先在项目目录双击 start.bat 装一次环境。"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {APP_NAME} =====\n")
    log.flush()

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    # 有的宿主（自动化/远程桌面会话）会给我们套 Job Object，退出时把子进程一起带走。
    # 先尝试让服务脱离作业独立存活，被拒绝再去掉这个标志。
    last_error: OSError | None = None
    for flags in (no_window | detached | breakaway, no_window | detached):
        try:
            subprocess.Popen(
                command,
                cwd=str(BASE),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
                env=_server_env(),
            )
            break
        except OSError as error:
            last_error = error
    else:
        return False, f"启动失败：{last_error}"

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if _healthy():
            return True, ""
        time.sleep(0.6)
    tail = _log_tail()
    return False, f"服务在 {START_TIMEOUT:.0f} 秒内没有就绪。\n\n日志末尾：\n{tail}"


def _log_tail(lines: int = 15) -> str:
    if not LOG_FILE.is_file():
        return "（没有日志）"
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "（日志读不了）"
    return "\n".join(content[-lines:])


def stop_server() -> tuple[bool, str]:
    pid = _pid_on_port()
    if pid is None:
        return True, "服务本来就没在跑。"
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"停止失败：{error}"
    for _ in range(20):
        if not _port_busy():
            return True, "服务已停止。"
        time.sleep(0.3)
    return False, f"进程 {pid} 还在，可能被别的程序占用。"


def open_app() -> None:
    webbrowser.open(LOCAL_URL)


def show_starting_page() -> bool:
    """先开一个「正在启动」页，服务就绪后它自己跳进工作台。

    启动要十几秒，这期间如果什么都不显示，用户会以为没点上而反复双击。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        page = LOG_DIR / "starting.html"
        page.write_text(STARTING_PAGE, encoding="utf-8")
        return bool(webbrowser.open(f"{page.as_uri()}?next={LOCAL_URL}"))
    except OSError:
        return False


# ------------------------------------------------------------------ 服务本体

def render_task() -> int:
    """--render 分支：单独一个进程跑一条片子。

    打包后 `sys.executable` 就是 exe 本身，没有解释器能接 `-m app.render_cli`，
    所以渲染入口也收进启动器里。结果不走 stdout——windowed 模式下它可能是
    None，父进程用一个 `--result` 指定的文件来接。
    """
    argv = sys.argv
    try:
        task_id = argv[argv.index("--render") + 1]
    except (ValueError, IndexError):
        _say("渲染入口缺少任务 ID。", MB_ICONERROR)
        return 2

    if "--result" in argv:
        try:
            result_path = Path(argv[argv.index("--result") + 1])
            result_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(result_path, "w", encoding="utf-8", errors="replace")
            sys.stdout = handle
            sys.stderr = handle
        except (OSError, IndexError, ValueError):
            pass

    os.environ.setdefault("OVA_DATA_DIR", str(DATA))
    try:
        from app.render_cli import main as render_main
    except Exception:  # noqa: BLE001 - 起不来也要把原因写进结果文件
        import traceback

        print(json.dumps({"ok": False, "error": traceback.format_exc()[-800:]}, ensure_ascii=False))
        return 1
    # render_cli.main 把 argv[1] 当任务 ID，所以这里塞一个占位符
    return render_main(["--render", task_id])


def serve() -> int:
    """--serve 分支：真正跑 uvicorn 的进程。

    单独一个进程的好处是关掉启动器不影响出片——一条片子要渲好几分钟，
    中途被关掉会让人以为程序坏了。
    """
    os.environ.setdefault("OVA_DATA_DIR", str(DATA))
    DATA.mkdir(parents=True, exist_ok=True)

    # 打包后 stdout 可能为 None（windowed 模式），任何 print 都会炸，
    # 所以统一兜一个文件句柄。
    if sys.stdout is None or sys.stderr is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handle = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
        sys.stdout = sys.stdout or handle
        sys.stderr = sys.stderr or handle

    try:
        import uvicorn

        from app.main import app
    except Exception:  # noqa: BLE001 - 起不来要给用户看得到的原因
        import traceback

        detail = traceback.format_exc()
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / "serve-crash.log").write_text(detail, encoding="utf-8")
        except OSError:
            pass
        print(detail, file=sys.stderr)
        _say(f"程序起不来，启动失败。\n\n详情已记录到：\n{DATA / 'tmp' / 'serve-crash.log'}", MB_ICONERROR)
        return 1

    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)
    return 0


# ------------------------------------------------------------------ 入口

def _ask_running() -> str:
    """已经在跑：让用户选打开还是停掉。返回 open / stop / close。"""
    answer = _say(
        "程序已经在运行了。\n\n是：打开工作台\n否：关掉服务",
        MB_YESNO | MB_ICONQUESTION,
    )
    if answer == IDYES:
        return "open"
    if answer == 7:  # IDNO
        return "stop"
    return "close"


def main() -> int:
    # PyInstaller 打包后如果程序里用到 multiprocessing，必须挡在最前面，
    # 否则子进程会把整个启动流程再跑一遍。
    try:
        import multiprocessing

        multiprocessing.freeze_support()
    except Exception:  # noqa: BLE001 - 没有这个模块不影响主流程
        pass

    if "--serve" in sys.argv:
        return serve()

    if "--render" in sys.argv:
        return render_task()

    if "--stop" in sys.argv:
        ok, detail = stop_server()
        _say(detail, MB_ICONINFO if ok else MB_ICONERROR)
        return 0 if ok else 1

    if _healthy():
        choice = _ask_running()
        if choice == "open":
            open_app()
        elif choice == "stop":
            ok, detail = stop_server()
            _say(detail, MB_ICONINFO if ok else MB_ICONERROR)
            return 0 if ok else 1
        return 0

    if _port_busy():
        _say(
            f"端口 {PORT} 被别的程序占着（进程 {_pid_on_port()}），但这个程序没在运行。\n\n"
            "请先把占用它的程序关掉，再双击一次。",
            MB_ICONERROR,
        )
        return 1

    showed_page = show_starting_page()
    ok, detail = start_server()
    if not ok:
        _say(detail, MB_ICONERROR)
        return 1
    # 「启动中」页会自己跳转；万一浏览器没打开它，这里兜一次直接进工作台
    if not showed_page:
        open_app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
