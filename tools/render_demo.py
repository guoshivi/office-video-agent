"""渲染一条风格成片 demo：模板「10.30 实地看房」+ 素材「10.30-卡点.mp4」。

用法：
    python tools/render_demo.py [audio_mode]
    audio_mode ∈ mix / original / voice / mute，默认 mix
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.copywriting import generate_storyboard
from app.db import connect
from app.style_render import render_storyboard

TEMPLATE_ID = "tpl_ff208998e792"      # 10.30 实地看房
SOURCE = r"D:\剪映成品视频\10.30-卡点.mp4"

FACTS = {
    "region": "民治",
    "area": "220",
    "partition": "3加1格局",
    "orientation": "南向",
    "furnishing_layout": "全套工桌工椅、老板间家具齐全",
    "notes": "高区楼层、双面采光、即入驻即办公",
}

# 角标跟着模板 shot 的 labels 走：句 3/4 是工区，句 6/7 是老板办公室，
# 句 9 是接待室。规则文案的 label 也会从模板带过来，这里兜底补全。
LABEL_OVERRIDES = {3: ("工区", "可容纳20人办公"), 6: ("老板办公室", "双面采光 配置齐全"), 9: ("接待室", "可做接待洽谈")}


def main() -> None:
    audio_mode = sys.argv[1] if len(sys.argv) > 1 else "mix"
    connection = connect()
    row = connection.execute(
        "SELECT spec_json FROM style_templates WHERE id=?", (TEMPLATE_ID,)
    ).fetchone()
    spec = json.loads(row["spec_json"])

    board, origin = generate_storyboard(spec, FACTS)
    print(f"文案来源：{origin}")
    for entry in board:
        print(f"  {entry['index']}. {entry['text']}")

    for index, (title, sub) in LABEL_OVERRIDES.items():
        if index <= len(board):
            board[index - 1]["label"] = {"title": title, "sub": sub}

    out = Path("data") / "renders" / f"demo_v2_{audio_mode}.mp4"
    started = time.time()
    render_storyboard(board, SOURCE, out, audio_mode=audio_mode)
    print(f"渲染完成：{out}（{time.time() - started:.1f}s）")


if __name__ == "__main__":
    main()
