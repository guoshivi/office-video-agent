#!/usr/bin/env python
"""体检房源表的租金写法：哪些能折成月租，哪些判不出来，各自按什么口径算的。

房源表是人手填的，租金一列有「40」「35+5」「900包物业」「面谈」七八种写法。
文案要报价格，就必须先知道每条房源被理解成了什么。与其靠人记口径，
不如把判别结论打出来：判定可疑的一眼就能看见，改 Excel 也有依据。

    python tools/rent_audit.py             # 看汇总
    python tools/rent_audit.py --list 20   # 顺带列出 20 条判不出来的
    python tools/rent_audit.py --json      # 出机器可读的完整结果
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.hooks import _first_number, explain_rent  # noqa: E402

DB = ROOT / "data" / "office_video_agent.db"


def rows(db: Path) -> list[sqlite3.Row]:
    if not db.exists():
        raise SystemExit(f"找不到数据库：{db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, region_raw, building_raw, room_raw, area_raw, rent_raw "
            "FROM properties ORDER BY region_raw, room_raw"
        ).fetchall()
    finally:
        conn.close()


def _reason(note: str) -> str:
    """把一句解释归成一个大类，汇总时才好数。"""
    for token, label in (
        ("没填租金", "没填租金"),
        ("缺面积", "缺面积，折不出单价"),
        ("认不出金额", "认不出金额（面谈/工位等）"),
        ("超出合理区间", "折算结果离谱，疑似填错列"),
        ("判不出", "判不出是单价还是总价"),
    ):
        if token in note:
            return label
    return note[:20] or "未说明"


def main() -> int:
    parser = argparse.ArgumentParser(description="房源租金写法体检")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--list", type=int, default=8, help="列出多少条判不出来的")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    report = []
    for row in rows(args.db):
        area = _first_number(row["area_raw"] or "")
        result = explain_rent({"rent_raw": row["rent_raw"] or ""}, area)
        result["area"] = area
        result["label"] = " ".join(
            str(row[key] or "").strip()
            for key in ("region_raw", "building_raw", "room_raw")
        ).strip() or row["id"][:8]
        report.append(result)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    total = len(report)
    ok = [r for r in report if r["ok"]]
    print(f"房源 {total} 条：能算出月租 {len(ok)} 条，跳过 {total - len(ok)} 条\n")

    units = collections.Counter(r["unit"] for r in ok)
    print("按判定口径：")
    for unit, count in units.most_common():
        print(f"  {unit:<12} {count:>4} 条")

    skipped = [r for r in report if not r["ok"]]
    if skipped:
        print("\n跳过的原因：")
        reasons = collections.Counter(_reason(r["note"]) for r in skipped)
        for reason, count in reasons.most_common():
            print(f"  {reason:<28} {count:>4} 条")

    # 抽几条能算出来的，确认数字像人话；再抽判不出来的，好照着改 Excel
    samples = sorted(ok, key=lambda r: -(r["monthly"] or 0))[:3]
    if samples:
        print("\n月租最高的三条（核对一下是不是真房源）：")
        for r in samples:
            print(
                f"  {r['label']:<22} {r['raw']:<12} 面积{r['area'] or '-':>6} "
                f"→ 月租 {r['monthly']:>9,.0f} 元（{r['unit']}）"
            )

    if args.list and skipped:
        print(f"\n判不出来的前 {args.list} 条：")
        for r in skipped[: args.list]:
            print(f"  {r['label']:<22} 租金「{r['raw']}」→ {r['note']}")

    print("\n报出去的只有 ok 的那些；跳过的房源照常出片，只是不说价格。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
