"""查看当前库内 Top 事件与评分明细，用于人工校验排序质量。"""
from __future__ import annotations

import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

con = sqlite3.connect("data/infotwitter.db")
rows = con.execute(
    """
    SELECT title_zh, canonical_title, event_type, category, impact_scope, authenticity,
           importance_total, score_breakdown, status, primary_url
    FROM stories ORDER BY importance_total DESC LIMIT 12
    """
).fetchall()

print(f"{'分数':>6} {'层级':<4} {'类型':<10} {'品类':<5} {'范围':<12} 标题")
print("-" * 100)
for r in rows:
    title = (r[0] or r[1] or "")[:44]
    print(f"{r[6]:>6} {r[5]:<4} {r[2]:<10} {r[3]:<5} {r[4]:<12} {title}")

print("\n--- 最高分条目的评分明细 ---")
if rows:
    b = json.loads(rows[0][7] or "{}")
    for k, v in b.items():
        print(f"  {k:<18} {v}")
    print(f"  url: {rows[0][9]}")
