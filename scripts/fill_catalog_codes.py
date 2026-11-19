# -*- coding: utf-8 -*-
"""给海外目录补 GW 官方货号（只读商品页的货号字段，不抓描述）。

为什么单独一条链路：货号是**精确匹配键**，比名字可靠。目录有 4000+ 条，
逐条进详情页约 2.5 秒/条，所以设计成可分多次、断点续跑的小批量任务。

用法：
    python scripts/fill_catalog_codes.py --limit 50
    python scripts/fill_catalog_codes.py            # 一直跑到补完
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import Database, now_iso  # noqa: E402

CODE_JS = """() => {
    // 零售站的货号有两种：GW 官方数字码（99120108075），
    // 以及第三方/自有商品的字母码（copinfbun）。两种都存，匹配时只用数字码。
    const body = document.body ? document.body.innerText : '';
    const m = body.match(/Product Code:\\s*(?:SKU\\s*\\/\\s*)?([A-Za-z0-9][A-Za-z0-9\\-]{2,20})/i);
    if (m) return m[1];
    const nodes = [...document.querySelectorAll('[class*="product-code"], [class*="sku"]')];
    for (const n of nodes) {
        const t = (n.innerText || '').trim();
        if (t.length >= 3 && t.length <= 30) return t.replace(/^\\W+/, '').slice(0, 30);
    }
    return '';
}"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--since-days", type=int, default=0,
                    help="只补最近 N 天新出现的目录行（0=全部）")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)

    # 已抓过详情的先直接复用，不重复请求
    reused = db.conn.execute(
        """
        UPDATE overseas_catalog
           SET product_code = (SELECT d.product_code FROM product_details d
                                WHERE d.url = overseas_catalog.url
                                  AND coalesce(d.product_code,'')<>'')
         WHERE coalesce(product_code,'')=''
           AND EXISTS (SELECT 1 FROM product_details d
                        WHERE d.url = overseas_catalog.url
                          AND coalesce(d.product_code,'')<>'')
        """
    ).rowcount
    db.conn.commit()
    if reused:
        print(f"从已抓详情复用 {reused} 条货号")

    # 只补战锤相关品类：目录里混进了桌游/卡牌（星战、万智牌、迪士尼），
    # 那些既不会匹配到 GW 货号，也没必要花时间去抓货号
    sql = (
        "SELECT url, title, first_seen_at FROM overseas_catalog"
        " WHERE coalesce(product_code,'')=''"
        "   AND (category_path LIKE '%warhammer-40k%'"
        "        OR category_path LIKE '%age-of-sigmar%'"
        "        OR category_path LIKE '%horus%'"
        "        OR category_path LIKE '%necromunda%'"
        "        OR category_path LIKE '%blood-bowl%'"
        "        OR category_path LIKE '%kill-team%'"
        "        OR category_path LIKE '%blacklibrary%')"
    )
    if args.since_days:
        from datetime import datetime, timedelta

        from infotwitter.db import CST

        since = (datetime.now(CST) - timedelta(days=args.since_days)).replace(
            microsecond=0).isoformat()
        sql += f" AND first_seen_at >= '{since}'"
        print(f"只看 {since[:10]} 之后新出现的")
    sql += " ORDER BY price DESC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = [dict(r) for r in db.conn.execute(sql)]
    if not rows:
        print("目录货号已补齐。")
        db.close()
        return 0
    print(f"待补货号 {len(rows)} 条")

    from playwright.sync_api import sync_playwright

    got = miss = fail = 0
    streak = 0          # 连续失败计数：站点宕机时别一个个等超时
    ABORT_AFTER = 5
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": cfg.proxy} if cfg.proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=cfg.user_agent, locale="en-GB",
                                  viewport={"width": 1440, "height": 1200})
        page = ctx.new_page()
        for i, r in enumerate(rows, 1):
            try:
                page.goto(r["url"], wait_until="domcontentloaded", timeout=35000)
                page.wait_for_timeout(1200)
                code = page.evaluate(CODE_JS) or ""
            except Exception:  # noqa: BLE001
                fail += 1
                streak += 1
                if streak >= ABORT_AFTER:
                    print(f"\n连续 {streak} 次失败，判定站点不可用，提前中止"
                          f"（已拿到 {got} 条，进度不会丢，下次接着补）")
                    browser.close()
                    db.close()
                    return 2
                continue
            streak = 0
            if code:
                with db.tx() as conn:
                    conn.execute("UPDATE overseas_catalog SET product_code=? WHERE url=?",
                                 (code, r["url"]))
                got += 1
            else:
                miss += 1
                with db.tx() as conn:
                    # 记成空串，避免下一轮又把它排到队首反复重试
                    conn.execute("UPDATE overseas_catalog SET product_code='' WHERE url=?",
                                 (r["url"],))
            if i % 25 == 0:
                print(f"  进度 {i}/{len(rows)}：拿到 {got}，页面无码 {miss}，失败 {fail}")
            time.sleep(args.sleep)
        browser.close()

    print(f"\n本次：拿到货号 {got}，页面没有码 {miss}，失败 {fail}")
    total = db.conn.execute(
        "SELECT COUNT(*) c FROM overseas_catalog WHERE coalesce(product_code,'')<>''"
    ).fetchone()["c"]
    print(f"目录累计有货号 {total} 条")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
