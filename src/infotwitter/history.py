"""历史推送汇总：把台账渲染成可读的表格，也支持导出 CSV。

台账数据本来就在 `pushes` 表里，但只有 `status` 能看到今天。
这里提供按天/按周期的汇总视图。
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .db import CST


def fetch_history(conn: sqlite3.Connection, days: int = 14, limit: int = 5000) -> list[dict[str, Any]]:
    days = max(1, min(int(days), 365))
    since = (datetime.now(CST) - timedelta(days=days)).date().isoformat()
    rows = conn.execute(
        """
        SELECT p.push_date, p.slot, p.story_id, p.title, p.importance,
               p.channel, p.sent_at, p.delivery_ok,
               s.event_type, s.category, s.authenticity, s.primary_url, s.canonical_title
        FROM pushes p
        LEFT JOIN stories s ON s.id = p.story_id
        WHERE p.push_date >= ?
        ORDER BY p.push_date DESC, p.slot ASC
        LIMIT ?
        """,
        (since, max(1, int(limit))),
    )
    return [dict(r) for r in rows]


def day_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    days: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = r["push_date"]
        bucket = days.setdefault(
            d, {"date": d, "count": 0, "ok": 0, "pending": 0, "top_score": 0.0,
                "has_product": False, "has_cn": False}
        )
        bucket["count"] += 1
        # delivery_ok: 0=失败 1=已送达 2=在途。在途只是"发出去了但没确认"，
        # 以前 `if delivery_ok` 会把它当成已送达（P2-5）
        if r["delivery_ok"] == 1:
            bucket["ok"] += 1
        elif r["delivery_ok"] == 2:
            bucket["pending"] += 1
        if r["importance"]:
            bucket["top_score"] = max(bucket["top_score"], float(r["importance"]))
        if r["slot"] == 5 and r["title"]:
            bucket["has_product"] = True
        if r["slot"] == 6:
            bucket["has_cn"] = True
    return days


def render_text(rows: list[dict[str, Any]], days: int) -> str:
    if not rows:
        return f"最近 {days} 天没有推送记录。"

    days_map = sorted(day_summary(rows).values(), key=lambda x: x["date"], reverse=True)
    lines = [f"最近 {days} 天共推送 {len(days_map)} 天、{len(rows)} 条\n", "=== 按天汇总 ==="]
    lines.append(f"{'日期':<12}{'条数':>4}{'送达':>5}{'最高分':>8}   商品槽  中文槽")
    for d in days_map:
        lines.append(
            f"{d['date']:<12}{d['count']:>4}{d['ok']:>5}{d['top_score']:>8.1f}   "
            f"{'有' if d['has_product'] else '—':<7}{'有' if d['has_cn'] else '—'}"
        )

    lines.append("")
    lines.append("=== 明细 ===")
    current = None
    for r in rows:
        if r["push_date"] != current:
            current = r["push_date"]
            lines.append(f"\n[{current}]")
        slot = r["slot"]
        mark = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥"}.get(slot, str(slot))
        score = f"{r['importance']:.1f}" if r["importance"] else "  — "
        tier = r["authenticity"] or ""
        title = (r["title"] or "")[:40]
        state = {0: "  [未送达]", 2: "  [在途/未确认]"}.get(r["delivery_ok"], "")
        lines.append(f"  {mark} {score:>5} {tier:<3} {title}{state}")
    return "\n".join(lines)


def export_csv(rows: list[dict[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "日期", "槽位", "标题", "原文标题", "分数", "可信度", "事件类型",
            "品类", "送达", "渠道", "发送时间", "链接",
        ])
        for r in rows:
            writer.writerow([
                r["push_date"], r["slot"], r["title"], r["canonical_title"],
                r["importance"], r["authenticity"], r["event_type"], r["category"],
                "OK" if r["delivery_ok"] else "FAIL", r["channel"],
                r["sent_at"], r["primary_url"],
            ])
    return len(rows)
