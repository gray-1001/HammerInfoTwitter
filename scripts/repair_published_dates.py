# -*- coding: utf-8 -*-
"""一次性修复：把浏览器源已入库文档的发布日期，按文章页 JSON-LD 重新校准。

用法：
    python scripts/repair_published_dates.py            # 只修没有日期或日期明显可疑的
    python scripts/repair_published_dates.py --all      # 该源全部重算
    python scripts/repair_published_dates.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.content import extract_date_published  # noqa: E402
from infotwitter.db import Database  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="whc_news", help="要修复的源 id")
    ap.add_argument("--all", action="store_true", help="该源全部重算，而不是只修缺失/可疑的")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)

    where = "source_id = ?"
    params: list = [args.source]
    if not args.all:
        # 只处理没有日期、或日期是抓取时刻的
        where += " AND (published_at IS NULL OR published_at = fetched_at)"
    rows = db.conn.execute(
        f"SELECT id, url, published_at FROM raw_docs WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    if not rows:
        print("没有需要修复的行。")
        db.close()
        return 0

    from playwright.sync_api import sync_playwright

    print(f"待修复 {len(rows)} 条（源 {args.source}）")
    fixed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": cfg.proxy} if cfg.proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(user_agent=cfg.user_agent, locale="en-GB")
            page = ctx.new_page()
            for r in rows:
                try:
                    page.goto(r["url"], wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(1200)
                    published = extract_date_published(page.content())
                except Exception as exc:  # noqa: BLE001
                    print(f"  ✗ {r['id']} 抓取失败 {type(exc).__name__}")
                    continue
                old = str(r["published_at"] or "（空）")[:19]
                if not published:
                    print(f"  ? {r['id']} 页面没有 datePublished，保持 {old}")
                    continue
                changed = old[:10] != published[:10]
                print(f"  {'✔' if changed else '='} {r['id']} {old} -> {published[:19]}")
                if changed:
                    fixed += 1
                    if not args.dry_run:
                        db.conn.execute(
                            "UPDATE raw_docs SET published_at=?, time_source='article' WHERE id=?",
                            (published, r["id"]),
                        )
        finally:
            browser.close()

    if not args.dry_run:
        db.conn.commit()
    print(f"\n修复 {fixed} 条" + ("（dry-run，未写入）" if args.dry_run else ""))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
