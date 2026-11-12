"""桌面启动器：双击即启动房源视频助手并打开工作台。

做成 exe 放桌面后，用户不需要记任何命令：
- 服务没跑 → 后台拉起 uvicorn，等健康检查通过，自动打开浏览器
- 服务已在跑 → 直接打开，或点「停止服务」关掉
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_NAME = "房源视频助手"
HOST = "0.0.0.0"
PORT = 8770
LOCAL_URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = f"{LOCAL_URL}/api/health"
START_TIMEOUT = 60.0


def base_dir() -> Path:
    """项目根目录。打包成 exe 后仍指向真实项目，不跟着 exe 跑。"""
    override = os.environ.get("OVA_HOME", "").strip()
    if override and Path(override).is_dir():
        return Path(override)
    # 源码方式运行时按文件位置推
    here = Path(__file__).resolve().parent.parent
    if (here / "app" / "main.py").is_file():
        return here
    return Path(r"D:\ova")


BASE = base_dir()
VENV_PY = BASE / ".venv" / "Scripts" / "python.exe"
LOG_DIR = BASE / "data" / "tmp"
LOG_FILE = LOG_DIR / "uvicorn.log"


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

def start_server() -> tuple[bool, str]:
    if not VENV_PY.is_file():
        return False, f"找不到 Python 环境：{VENV_PY}"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {APP_NAME} =====\n")
    log.flush()
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    command = [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)]
    # 有的宿主（自动化/远程桌面会话）会给我们套 Job Object，退出时把子进程一起带走。
    # 先尝试让服务脱离作业独立存活，被拒绝再去掉这个标志。
    for flags in (no_window | detached | breakaway, no_window | detached):
        try:
            subprocess.Popen(
                command, cwd=str(BASE),
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=flags, close_fds=True,
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


def _log_tail(lines: int = 12) -> str:
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
    import webbrowser

    webbrowser.open(LOCAL_URL)


# ------------------------------------------------------------------ 界面

def _ui():
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("360x150")
    root.resizable(False, False)
    root.configure(bg="#11141a")
    tk.Label(root, text=APP_NAME, font=("Microsoft YaHei UI", 14, "bold"),
             bg="#11141a", fg="#f4f6fb").pack(pady=(22, 6))
    status = tk.Label(root, text="正在准备…", font=("Microsoft YaHei UI", 10),
                      bg="#11141a", fg="#9aa4b8")
    status.pack()

    # 居中
    root.update_idletasks()
    x = (root.winfo_screenwidth() - 360) // 2
    y = (root.winfo_screenheight() - 150) // 2 - 60
    root.geometry(f"+{x}+{y}")
    return root, status, messagebox


def _ask_running(messagebox) -> str:
    """已经在跑：让用户选打开还是停掉。返回 open / stop / close。"""
    answer = messagebox.askyesnocancel(
        APP_NAME,
        "服务已经在运行了。\n\n是(Y) 打开工作台\n否(N) 停止服务\n取消 什么都不做",
        parent=None,
    )
    return {True: "open", False: "stop", None: "close"}[answer]


def main() -> int:
    if _healthy():
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        choice = _ask_running(messagebox)
        root.destroy()
        if choice == "open":
            open_app()
        elif choice == "stop":
            ok, detail = stop_server()
            root2 = tk.Tk()
            root2.withdraw()
            messagebox.showinfo(APP_NAME, detail)
            root2.destroy()
            return 0 if ok else 1
        return 0

    root, status, messagebox = _ui()
    result: dict[str, object] = {}

    def worker() -> None:
        if _port_busy():
            result["error"] = (
                f"端口 {PORT} 被别的程序占着（进程 {_pid_on_port()}），"
                "但健康检查没通过。\n请先关掉那个程序再试。"
            )
            return
        status.config(text="正在启动服务，请稍候…")
        ok, detail = start_server()
        result["ok"] = ok
        result["detail"] = detail

    import threading

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def poll() -> None:
        if thread.is_alive():
            root.after(300, poll)
            return
        if result.get("ok"):
            status.config(text="已就绪，正在打开工作台…")
            open_app()
            root.after(700, root.destroy)
        else:
            root.withdraw()
            messagebox.showerror(APP_NAME, str(result.get("error") or result.get("detail") or "启动失败"))
            root.destroy()

    root.after(300, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
