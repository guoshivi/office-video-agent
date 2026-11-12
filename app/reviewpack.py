"""读图包：把一条参考视频摊成「一张镜头拼图 + 一份待填清单」，交给 WorkBuddy 复核。

为什么要有这一层：

- 本地能算的（切点、时长、OCR 台词）交给脚本，快且免费；
- 本地算不准的（这一镜拍的是什么、台词有没有听错、开头凭什么抓人）交给会看图的人。
  不是"再调一个模型"，而是把视频压成一张图，让本来就在会话里的 WorkBuddy 直接看。

读图包不改库，只落在模板目录里；复核结果由 `apply_review` 合并回 spec。
这样即使没人复核，模板也仍然可用（只是角色是骨架猜的）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .asr import _windows_path
from .db import json_dumps, transaction, utc_now
from .refvideo import RefVideoError, analyze_file
from .shots import _find_binary, probe_duration

# 拼图每格宽度：720 宽的竖屏缩到 240 还能看清字幕，再小就糊了
CELL_WIDTH = 240
COLUMNS = 4
MAX_CELLS = 12

REVIEW_FIELDS = (
    "subject",   # 这一镜拍的是什么：门头 / 开放工位 / 老板间 / 会议室 / 走廊 / 窗外
    "line",      # 这一镜的台词（照抄画面字幕，没有就留空）
    "role",      # hook / area / zone / furn / boss / value / cta
    "why",       # 这一镜为什么放在这个位置（可选，只对 hook 有用）
)


class ReviewPackError(RuntimeError):
    pass


# ------------------------------------------------------------------ 拼图

def _frame_at(path: Path, seconds: float, target: Path) -> bool:
    ffmpeg = _find_binary("ffmpeg")
    completed = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(seconds, 0):.2f}", "-i", _windows_path(path),
            "-frames:v", "1", "-vf", f"scale={CELL_WIDTH}:-1", str(target),
        ],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and target.is_file()


def scene_spans(duration: float, cuts: list[float]) -> list[tuple[float, float]]:
    """切点 → 镜头区间。拼图按镜头抽帧，不按台词句抽帧。

    这两套时间轴必须分开：一条 25 秒的片子可能只有 8 个镜头，却有 20 多句台词。
    按台词抽帧会抽出同一个镜头的十几个瞬间，拼图等于没用。
    """
    from .refvideo import _merge_close

    edges = [0.0] + [c for c in _merge_close(cuts or []) if 0.2 < c < duration - 0.2] + [duration]
    spans = [(a, b) for a, b in zip(edges, edges[1:]) if b - a > 0.3]
    return spans or [(0.0, duration)]


def build_grid(path: Path, spans: list[tuple[float, float]], out: Path) -> Path:
    """按镜头区间中点抽一帧，拼成带序号和时长的对照图。

    序号很重要：复核时说的"第 3 镜"必须和图上的 3 对得上，
    否则回填时会把角色按错位置写进去。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError as error:  # pragma: no cover - Pillow 是出片依赖，随包
        raise ReviewPackError("缺少 Pillow，无法生成镜头拼图") from error

    # 镜头多于 12 个时均匀取 12 个，宁可漏几个也要保住每格能看清
    if len(spans) > MAX_CELLS:
        step = len(spans) / MAX_CELLS
        spans = [spans[int(index * step)] for index in range(MAX_CELLS)]
    if not spans:
        raise ReviewPackError("没有可用的镜头，无法生成拼图")

    out.parent.mkdir(parents=True, exist_ok=True)
    cells: list[tuple[Image.Image, int, float]] = []
    for index, (start, end) in enumerate(spans, start=1):
        middle = (start + end) / 2
        temp = out.parent / f"_cell_{index:02d}.jpg"
        if not _frame_at(path, middle, temp):
            continue
        cells.append((Image.open(temp).convert("RGB"), index, end - start))
        temp.unlink(missing_ok=True)

    if not cells:
        raise ReviewPackError("抽帧失败，视频可能损坏")

    width, height = cells[0][0].size
    rows = (len(cells) + COLUMNS - 1) // COLUMNS
    canvas = Image.new("RGB", (COLUMNS * width, rows * (height + 26)), "white")
    draw = ImageDraw.Draw(canvas)
    for position, (image, index, seconds) in enumerate(cells):
        x = (position % COLUMNS) * width
        y = (position // COLUMNS) * (height + 26)
        canvas.paste(image, (x, y))
        caption = f"#{index} {seconds:.1f}s"
        # 白底黑字压在格子上方，避免深色画面里看不见
        draw.rectangle([x, y + height, x + width, y + height + 26], fill="white")
        draw.text((x + 6, y + height + 6), caption, fill="black")
    canvas.save(out, quality=88)
    return out


# ------------------------------------------------------------------ 清单

def _scene_of(second: float, spans: list[tuple[float, float]]) -> int:
    for index, (start, end) in enumerate(spans, start=1):
        if start <= second < end:
            return index
    return len(spans)


def build_checklist(
    spec: dict, name: str = "", spans: list[tuple[float, float]] | None = None
) -> dict:
    """把 spec 摊成一份待填清单。

    预填两栏，复核时只需要改错的：
    - line：OCR 已经读出的一版台词（可能带噪声，所以要人看一眼）
    - role：骨架按位置猜的角色（没有台词时全是猜的）

    subject 一律留空——本地没有任何能力判断"这镜拍的是什么"，猜不如空着。
    """
    spans = spans or scene_spans(float(spec.get("duration") or 0), spec.get("cuts") or [])

    shots = []
    for shot in spec.get("shots") or []:
        shots.append({
            "index": shot["index"],
            "scene": shot.get("scene") or _scene_of(float(shot.get("start") or 0), spans),
            "start": shot.get("start"),
            "seconds": shot["seconds"],
            "subject": "",
            "line": shot.get("sample", "") if spec.get("text_source") in {"caption", "review"} else "",
            "role": shot.get("role", ""),
            "why": "",
        })
    return {
        "name": name,
        "duration": spec.get("duration"),
        "text_source": spec.get("text_source", "none"),
        "next_step": (
            "把这份文件交回 WorkBuddy：说明要看 grid.jpg，按 shots 逐镜填 subject/line/role，"
            "然后调用 apply_pack 合并。"
        ),
        "fields": list(REVIEW_FIELDS),
        "scenes": [
            {"scene": index, "start": round(start, 2), "seconds": round(end - start, 1)}
            for index, (start, end) in enumerate(spans, start=1)
        ],
        "shots": shots,
    }


def pack_dir(template_id: str) -> Path:
    from .style_template import TEMPLATE_DIR

    return TEMPLATE_DIR / template_id


def build_pack(template_id: str, *, force: bool = False) -> dict:
    """生成读图包：拼图 + checklist.json。已有 spec 就复用，不重跑拆解。"""
    directory = pack_dir(template_id)
    with transaction() as connection:
        row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise ReviewPackError("模板不存在")

    source = Path(row["source_path"])
    if not source.is_file():
        raise ReviewPackError("参考视频文件已丢失，请重新上传")

    spec: dict = {}
    if row["spec_json"] and not force:
        try:
            spec = json.loads(row["spec_json"])
        except ValueError:
            spec = {}
    if not spec.get("shots"):
        try:
            spec = analyze_file(source)
        except RefVideoError as error:
            raise ReviewPackError(str(error)) from error
        with transaction() as connection:
            connection.execute(
                """UPDATE style_templates SET status='ready', spec_json=?, duration_s=?,
                   error='', updated_at=? WHERE id=?""",
                (
                    json_dumps(spec),
                    spec.get("duration") or probe_duration(source),
                    utc_now(),
                    template_id,
                ),
            )

    directory.mkdir(parents=True, exist_ok=True)
    duration = float(spec.get("duration") or probe_duration(source))
    spans = scene_spans(duration, spec.get("cuts") or [])
    # 给每句标上属于第几镜：复核按镜头给语义，回填时得知道下发给谁
    for shot in spec.get("shots") or []:
        shot["scene"] = _scene_of(float(shot.get("start") or 0), spans)
    with transaction() as connection:
        connection.execute(
            "UPDATE style_templates SET spec_json=? WHERE id=?", (json_dumps(spec), template_id)
        )
    grid = build_grid(source, spans, directory / "grid.jpg")
    checklist = build_checklist(spec, name=row["name"], spans=spans)
    (directory / "checklist.json").write_text(
        json.dumps(checklist, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "template_id": template_id,
        "grid": str(grid),
        "checklist": str(directory / "checklist.json"),
        "shots": len(checklist["shots"]),
        "text_source": spec.get("text_source", "none"),
        "warnings": spec.get("warnings") or [],
    }


# ------------------------------------------------------------------ 合并

def apply_review(
    template_id: str,
    *,
    scenes: list[dict] | None = None,
    shots: list[dict] | None = None,
    summary: str = "",
) -> dict:
    """把复核结果合并回 spec。

    分两层，因为看图得到的信息本来就是两层的：

    - `scenes` 按镜头给语义：这镜拍的是什么、在整条片子里担什么角色。
      同一镜里的所有台词都继承这个角色。
    - `shots` 按句子纠台词：改错字、并碎片、标 `drop` 删掉噪声句。

    没填的字段一律保留原值，漏填不会把已经拆出来的东西清掉。
    """
    with transaction() as connection:
        row = connection.execute("SELECT * FROM style_templates WHERE id=?", (template_id,)).fetchone()
    if row is None:
        raise ReviewPackError("模板不存在")

    spec = json.loads(row["spec_json"] or "{}")
    shots_list = spec.get("shots") or []
    if not shots_list:
        raise ReviewPackError("模板还没有分镜结构，先跑一次拆解")

    # 第一层：镜头语义
    scene_map = {int(item.get("scene") or 0): item for item in (scenes or []) if int(item.get("scene") or 0)}
    touched = 0
    if scene_map:
        for shot in shots_list:
            item = scene_map.get(int(shot.get("scene") or 0))
            if not item:
                continue
            subject = str(item.get("subject") or "").strip()
            role = str(item.get("role") or "").strip()
            why = str(item.get("why") or "").strip()
            if subject:
                shot["subject"] = subject[:20]
            if role:
                shot["role"] = role
            if why:
                shot["why"] = why[:80]
            touched += 1
        # 镜头语义单独存一份：短的收尾镜可能一句台词都没有，
        # 只挂在台词上的角色会整镜消失，结构占比就缺了一块
        spec["scenes"] = [
            {
                "scene": index,
                "subject": str(item.get("subject") or "").strip()[:20],
                "role": str(item.get("role") or "").strip(),
                "why": str(item.get("why") or "").strip()[:80],
            }
            for index, item in sorted(scene_map.items())
        ]

    # 第二层：逐句纠台词
    by_index = {int(shot["index"]): shot for shot in shots_list}
    dropped: set[int] = set()
    for item in shots or []:
        index = int(item.get("index") or 0)
        shot = by_index.get(index)
        if shot is None:
            continue
        if item.get("drop"):
            dropped.add(index)
            continue
        line = str(item.get("line") or "").strip()
        if line:
            shot["sample"] = line
            shot["words"] = len(line)
            touched += 1

    if dropped:
        shots_list = [shot for shot in shots_list if int(shot["index"]) not in dropped]
        for position, shot in enumerate(shots_list, start=1):
            shot["index"] = position
        spec["shots"] = shots_list

    duration = spec.get("duration") or row["duration_s"] or 0
    # 结构占比按**镜头时长**算，不按台词时长：结构讲的是"钩子占了开头几秒"，
    # 台词只在有人说话时才有时间，拿它当分母会算出一条片子只有一半时长
    spans = scene_spans(float(duration or 0), spec.get("cuts") or [])
    role_of_scene: dict[int, str] = {
        int(item["scene"]): item["role"] for item in spec.get("scenes") or [] if item.get("role")
    }
    for shot in shots_list:
        scene = int(shot.get("scene") or 0)
        if scene and scene not in role_of_scene and shot.get("role"):
            role_of_scene[scene] = shot["role"]

    shares: dict[str, float] = {}
    for scene, (start, end) in enumerate(spans, start=1):
        role = role_of_scene.get(scene)
        if role:
            shares[role] = round(shares.get(role, 0) + (end - start), 1)
    if duration:
        spec["structure"] = [
            {"role": role, "share": round(seconds / duration, 3), "seconds": seconds}
            for role, seconds in sorted(shares.items(), key=lambda item: -item[1])
        ]

    if summary:
        spec["summary"] = summary
    reviewed = sum(1 for shot in shots_list if shot.get("subject") or shot.get("why"))
    spec["reviewed"] = reviewed
    if any(shot.get("sample") for shot in shots_list):
        spec["text_source"] = "review"

    with transaction() as connection:
        connection.execute(
            "UPDATE style_templates SET spec_json=?, transcript=?, updated_at=? WHERE id=?",
            (
                json_dumps(spec),
                "\n".join(shot.get("sample", "") for shot in shots_list if shot.get("sample")),
                utc_now(),
                template_id,
            ),
        )
    return {
        "template_id": template_id,
        "shots": len(shots_list),
        "dropped": len(dropped),
        "reviewed": reviewed,
        "structure": spec.get("structure"),
    }
