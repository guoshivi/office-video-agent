#!/usr/bin/env python
"""房源视频助手的命令行入口。

WorkBuddy 桌面、飞书/企业微信里的会话、朋友自己开终端，都走这一个脚本。
它只调本地 HTTP 接口（默认 127.0.0.1:8770），不直接碰数据库，
所以服务跑在另一台机器上时加 `--api` 也能用。

典型对话：
    ova send ./office.mp4              # 先把视频放进来
    ova bind "A区 中关村大厦 1201"      # 再说房源，自动生成口播
    ova render V20261101-ABCDE --wait  # 出片并等结果
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

DEFAULT_API = os.getenv("OVA_API", "http://127.0.0.1:8770")


class CliError(RuntimeError):
    pass


def client(api: str) -> httpx.Client:
    return httpx.Client(base_url=api.rstrip("/"), timeout=120.0, trust_env=False)


# ------------------------------------------------------------------ 输出

def emit(payload, as_json: bool, human: str = "") -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif human:
        print(human)


def fail(message: str, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps({"error": message}, ensure_ascii=False, indent=2))
    else:
        print(f"✗ {message}", file=sys.stderr)
    return 1


# ------------------------------------------------------------------ 工具

def api_error(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except Exception:
        detail = None
    return str(detail or f"HTTP {response.status_code}")


def find_task(http: httpx.Client, reference: str) -> dict:
    """接受任务编号（V20261101-ABCDE）或内部 id。"""
    reference = reference.strip()
    response = http.get("/api/tasks", params={"limit": 200})
    if response.status_code != 200:
        raise CliError(api_error(response))
    items = response.json().get("items", [])
    for item in items:
        if item["id"] == reference or item["task_no"].upper() == reference.upper():
            return item
    raise CliError(f"找不到任务「{reference}」，可用 ova list 查看最近任务")


def stage_video(http: httpx.Client, source: str) -> tuple[Path, bool]:
    """本地文件直接用；http(s) 链接先下到临时目录（IM 附件常常只有链接）。"""
    if source.startswith(("http://", "https://")):
        temp_dir = Path(os.getenv("TEMP") or os.getenv("OVA_TMP") or ".")
        target = Path(temp_dir) / f"ova-download-{int(time.time())}.mp4"
        with http.stream("GET", source) as response:
            if response.status_code != 200:
                raise CliError(f"下载视频失败：HTTP {response.status_code}")
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
        return target, True
    path = Path(source).expanduser()
    if not path.is_file():
        raise CliError(f"视频文件不存在：{source}")
    return path, False


def human_size(num: int) -> str:
    megabytes = num / 1024 / 1024
    return f"{megabytes:.1f} MB" if megabytes >= 1 else f"{num / 1024:.0f} KB"


def describe_task(task: dict) -> str:
    lines = [
        f"任务 {task['task_no']}　{task.get('property_label', '')}",
        f"状态 {task['status']}　进度 {task.get('progress', 0)}%　{task.get('stage', '')}",
    ]
    if task.get("output_video_path"):
        lines.append(f"成片 {task['output_video_path']}")
    if task.get("error_message"):
        lines.append(f"错误 {task['error_message']}")
    return "\n".join(line for line in lines if line.strip())


def describe_candidates(candidates: list[dict]) -> str:
    rows = []
    for item in candidates:
        flag = "" if item["status"] == "active" else f"（{item['status']}）"
        rows.append(f"  {item['index']}. {item['label']}{flag}")
    return "\n".join(rows)


# ------------------------------------------------------------------ 子命令

def cmd_status(http: httpx.Client, args) -> int:
    health = http.get("/api/health").json()
    dashboard = http.get("/api/dashboard").json()
    workbench = http.get("/api/workbench/status").json()
    pending = http.get("/api/channel/clips", params={"status": "pending", "limit": 5}).json()["items"]
    payload = {"health": health, "dashboard": dashboard, "workbench": workbench, "pending_clips": pending}
    human = [
        f"服务 {args.api}　{health.get('status', '?')}　版本 {health.get('version', '?')}",
        f"房源 {dashboard.get('property_count', 0)} 条，待确认 {dashboard.get('review_count', 0)} 条",
        f"视频工作台 {'在线' if workbench.get('online') else '离线'}"
        + ("（正在出片）" if workbench.get("busy") else ""),
        f"待绑定视频 {len(pending)} 段",
    ]
    for clip in pending:
        human.append(f"  · {clip['id']}　{clip['filename']}　{human_size(clip['size'])}")
    emit(payload, args.json, "\n".join(human))
    return 0


def cmd_send(http: httpx.Client, args) -> int:
    path, temporary = stage_video(http, args.video)
    try:
        with path.open("rb") as handle:
            response = http.post(
                "/api/channel/clips",
                files={"video": (path.name, handle, "video/mp4")},
                data={"channel": args.channel, "sender": args.sender},
            )
    finally:
        if temporary:
            path.unlink(missing_ok=True)
    if response.status_code != 200:
        return fail(api_error(response), args.json)
    payload = response.json()
    clip = payload["clip"]
    human = (
        f"✓ 已收下视频　{clip['filename']}　{human_size(clip['size'])}\n"
        f"  编号 {clip['id']}\n"
        f"  {payload['hint']}"
    )
    emit(payload, args.json, human)
    return 0


def cmd_bind(http: httpx.Client, args) -> int:
    clip_id = args.clip
    if not clip_id:
        pending = http.get(
            "/api/channel/clips",
            params={"status": "pending", "channel": args.channel, "limit": 1},
        ).json()["items"]
        if not pending:
            return fail("没有待绑定的视频，请先用 ova send 发一段视频", args.json)
        clip_id = pending[0]["id"]

    body = {
        "query": args.query or "",
        "property_id": args.property_id or "",
        "target_duration": args.duration,
        "aspect_ratio": args.ratio,
        "voice": args.voice,
        "style": args.style,
    }

    # 先解析一次，人选的序号在这里换成 property_id，服务端保持无状态
    if not args.property_id and args.query:
        resolved = http.get("/api/channel/resolve", params={"q": args.query}).json()
        if resolved["status"] == "ambiguous" and args.pick:
            index = args.pick - 1
            if not 0 <= index < len(resolved["candidates"]):
                return fail(f"序号超出范围，共 {len(resolved['candidates'])} 个候选", args.json)
            body["property_id"] = resolved["candidates"][index]["property_id"]
            body["query"] = ""

    response = http.post(f"/api/channel/clips/{clip_id}/bind", json=body)
    if response.status_code != 200:
        return fail(api_error(response), args.json)
    payload = response.json()

    if payload["status"] == "bound" or payload["status"] == "already_bound":
        task = payload["task"]
        human = [
            f"✓ 已绑定　{payload.get('property', {}).get('label', task.get('property_label', ''))}",
            f"  任务 {task['task_no']}",
            "",
            "口播草稿：",
            task["script"],
        ]
        if task.get("warnings"):
            human.append("")
            human.append("提醒：")
            for warning in task["warnings"]:
                human.append(f"  · {warning}")
        human.append("")
        human.append(f"确认无误就出片：ova render {task['task_no']} --wait")
        emit(payload, args.json, "\n".join(human))
        return 0

    if payload["status"] == "ambiguous":
        human = [
            f"「{args.query}」匹配到 {len(payload['candidates'])} 个房源，选一个：",
            describe_candidates(payload["candidates"]),
            "",
            f"用 ova bind \"{args.query}\" --pick 序号 指定；或者把区域/楼栋写全一点",
        ]
        emit(payload, args.json, "\n".join(human))
        return 0

    hint = payload.get("hint") or "没有匹配到房源"
    fail(hint, args.json)
    if not args.json and payload.get("candidates"):
        print(describe_candidates(payload["candidates"]))
    return 1


def cmd_go(http: httpx.Client, args) -> int:
    """一条消息里同时给了视频和房源：收片 → 绑定 →（可选）直接出片。"""
    if not args.video:
        return fail("用法：ova go <视频> \"<房源>\" [--render]", args.json)
    if not args.query:
        return fail("用法：ova go <视频> \"<房源>\" [--render]", args.json)

    path, temporary = stage_video(http, args.video)
    try:
        with path.open("rb") as handle:
            response = http.post(
                "/api/channel/clips",
                files={"video": (path.name, handle, "video/mp4")},
                data={"channel": args.channel, "sender": args.sender},
            )
    finally:
        if temporary:
            path.unlink(missing_ok=True)
    if response.status_code != 200:
        return fail(api_error(response), args.json)
    clip = response.json()["clip"]

    resolved = http.get("/api/channel/resolve", params={"q": args.query}).json()
    body = {
        "property_id": args.property_id or "",
        "target_duration": args.duration,
        "aspect_ratio": args.ratio,
        "voice": args.voice,
        "style": args.style,
    }
    if not args.property_id:
        if resolved["status"] == "matched":
            body["property_id"] = resolved["property"]["property_id"]
        elif resolved["status"] == "ambiguous":
            if not args.pick:
                human = [
                    f"已收下视频，但「{args.query}」匹配到 {len(resolved['candidates'])} 个房源：",
                    describe_candidates(resolved["candidates"]),
                    "",
                    "视频已暂存（编号 " + clip["id"] + "），选好后：",
                    f'  ova bind "{args.query}" --pick 序号',
                ]
                emit({"clip": clip, **resolved}, args.json, "\n".join(human))
                return 0
            index = args.pick - 1
            if not 0 <= index < len(resolved["candidates"]):
                return fail(f"序号超出范围，共 {len(resolved['candidates'])} 个候选", args.json)
            body["property_id"] = resolved["candidates"][index]["property_id"]
        else:
            emit({"clip": clip, **resolved}, args.json, resolved.get("hint", "没有匹配到房源"))
            return 1

    bound = http.post(f"/api/channel/clips/{clip['id']}/bind", json=body)
    if bound.status_code != 200:
        return fail(api_error(bound), args.json)
    payload = bound.json()
    if payload["status"] not in {"bound", "already_bound"}:
        emit(payload, args.json, payload.get("hint", "绑定失败"))
        return 1

    task = payload["task"]
    human = [
        f"✓ {payload.get('property', {}).get('label', task.get('property_label', ''))}　任务 {task['task_no']}",
        "",
        "口播草稿：",
        task["script"],
    ]
    for warning in task.get("warnings") or []:
        human.append(f"· {warning}")
    if args.render:
        human.append("")
        human.append("开始出片…")
        if not args.json:
            print("\n".join(human))
        args.task = task["task_no"]
        return cmd_render(http, args)
    human.append("")
    human.append(f"确认无误就出片：ova render {task['task_no']} --wait")
    emit(payload, args.json, "\n".join(human))
    return 0


def cmd_list(http: httpx.Client, args) -> int:
    if args.pending:
        items = http.get("/api/channel/clips", params={"status": "pending", "limit": args.limit}).json()["items"]
        human = [f"待绑定视频 {len(items)} 段"]
        for clip in items:
            human.append(f"  · {clip['id']}　{clip['filename']}　{human_size(clip['size'])}　{clip['created_at'][:16]}")
        emit({"items": items}, args.json, "\n".join(human))
        return 0

    items = http.get("/api/tasks", params={"limit": args.limit}).json()["items"]
    if args.json:
        emit({"items": items}, True)
        return 0
    if not items:
        print("还没有任务")
        return 0
    for task in items:
        marker = {"completed": "✓", "failed": "✗", "processing": "…"}.get(task["status"], "·")
        print(f"{marker} {task['task_no']}　{task['status']:<11} {task.get('progress', 0):>3}%　{task.get('property_label', '')}")
    return 0


def cmd_show(http: httpx.Client, args) -> int:
    task = find_task(http, args.task)
    emit(task, args.json, describe_task(task) + "\n\n口播：\n" + task.get("script", ""))
    return 0


def cmd_script(http: httpx.Client, args) -> int:
    task = find_task(http, args.task)
    if args.set:
        response = http.put(f"/api/tasks/{task['id']}/script", json={"script": args.set})
        if response.status_code != 200:
            return fail(api_error(response), args.json)
        task = response.json()
    emit(task, args.json, f"✓ 已更新口播（{task['task_no']}）\n\n{task['script']}\n\n出片：ova render {task['task_no']} --wait")
    return 0


def cmd_render(http: httpx.Client, args) -> int:
    task = find_task(http, args.task)
    response = http.post(f"/api/tasks/{task['id']}/render")
    if response.status_code not in (200, 202):
        return fail(api_error(response), args.json)
    if not args.wait:
        emit(response.json(), args.json, f"… 已提交出片　{task['task_no']}\n  用 ova show {task['task_no']} 看进度")
        return 0

    deadline = time.time() + args.timeout
    last = ""
    started = time.time()
    while time.time() < deadline:
        time.sleep(5)
        current = http.get(f"/api/tasks/{task['id']}").json()
        stage = f"{current['status']} {current.get('progress', 0)}% {current.get('stage', '')}"
        if not args.json and stage != last:
            elapsed = int(time.time() - started)
            print(f"  [{elapsed:>3}s] {current.get('progress', 0):>3}%  {current.get('stage', current['status'])}", flush=True)
            last = stage
        if current["status"] == "completed":
            emit(current, args.json, f"\n✓ 出片完成　{current['output_video_path']}")
            return 0
        if current["status"] == "failed":
            return fail(f"出片失败：{current.get('error_message') or current.get('stage') or '未知原因'}", args.json)
    return fail(f"等待超过 {args.timeout} 秒，任务还在跑；用 ova show {task['task_no']} 继续看", args.json)


def cmd_out(http: httpx.Client, args) -> int:
    task = find_task(http, args.task)
    if not task.get("output_video_path"):
        return fail(f"任务 {task['task_no']} 还没有成片", args.json)
    emit(task, args.json, task["output_video_path"])
    return 0


def cmd_help(http: httpx.Client, args) -> int:
    print(__doc__.strip())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="?", default="status")
    parser.add_argument("argument", nargs="?")
    parser.add_argument("argument2", nargs="?")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--channel", default="desktop")
    parser.add_argument("--sender", default="")
    parser.add_argument("--clip", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--property-id", dest="property_id", default="")
    parser.add_argument("--pick", type=int, default=0)
    parser.add_argument("--duration", type=int, default=45)
    parser.add_argument("--ratio", default="9:16")
    parser.add_argument("--voice", default="female_warm")
    parser.add_argument("--style", default="professional")
    parser.add_argument("--set", default="")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--pending", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    # 「ova send x.mp4」里的 x.mp4 落在 argument 上，按命令搬运
    if args.command in {"send", "video"} and args.argument:
        args.video = args.argument
    elif args.command == "go":
        args.video = args.argument or ""
        args.query = args.argument2 or args.query
    elif args.command in {"bind", "房源"} and args.argument:
        args.query = args.argument
    elif args.command in {"show", "render", "out", "script", "状态", "出片"} and args.argument:
        args.task = args.argument
    for attribute in ("video", "task"):
        if not hasattr(args, attribute):
            setattr(args, attribute, "")

    handlers = {
        "status": cmd_status,
        "send": cmd_send,
        "video": cmd_send,
        "bind": cmd_bind,
        "房源": cmd_bind,
        "go": cmd_go,
        "list": cmd_list,
        "show": cmd_show,
        "状态": cmd_show,
        "script": cmd_script,
        "render": cmd_render,
        "出片": cmd_render,
        "out": cmd_out,
        "help": cmd_help,
    }
    handler = handlers.get(args.command)
    if handler is None:
        print(f"未知命令：{args.command}\n", file=sys.stderr)
        return cmd_help(http=None, args=args) or 2

    try:
        with client(args.api) as http:
            return handler(http, args)
    except httpx.ConnectError:
        return fail(f"连不上本地服务 {args.api}，请先启动 start.bat", args.json)
    except CliError as error:
        return fail(str(error), args.json)
    except httpx.HTTPError as error:
        return fail(f"请求失败：{error}", args.json)


if __name__ == "__main__":
    raise SystemExit(main())
