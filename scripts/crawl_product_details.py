# -*- coding: utf-8 -*-
"""抓商品详情（描述 / 件数 / 库存 / 货号），落 product_details。

只抓已经匹配上 GW 货号的商品——没匹配上的抓了也没法用，等匹配补上再跑一次即可
（脚本按 product_id upsert，可反复跑）。

用法：
    python scripts/crawl_product_details.py --limit 20 --dry-run
    python scripts/crawl_product_details.py --limit 50
    python scripts/crawl_product_details.py              # 全部已匹配的
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
from infotwitter.pricing.importers import _iso_week  # noqa: E402

DETAIL_JS = """() => {
    const out = {desc: '', code: '', title: document.title};
    const sels = ['#description', '.product-description', '[itemprop="description"]',
                  '.tab-content', '#tab-description', 'main'];
    for (const s of sels) {
        const n = document.querySelector(s);
        if (n && n.innerText && n.innerText.trim().length > 80) {
            out.desc = n.innerText.trim();
            break;
        }
    }
    const codeNode = document.querySelector('[class*="product-code"], [class*="sku"]');
    if (codeNode) out.code = (codeNode.innerText || '').trim();
    return out;
}"""


def clean_desc(text: str) -> str:
    """去掉运费限制段落与页脚噪声，只留商品描述。"""
    t = re.sub(r"\s+", " ", text or "")
    # "PRODUCT CAN BE SHIPPED TO: ..." 这段是物流限制，不是商品信息
    t = re.sub(r"PRODUCT CAN BE SHIPPED TO:.*?\.(?=\s|$)", " ", t, flags=re.I)
    t = t.split("PRODUCT DESCRIPTION")[-1] if "PRODUCT DESCRIPTION" in t else t
    return t.strip()[:2000]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)
    sql = ("SELECT c.product_id, c.url, c.title FROM overseas_catalog c"
           " LEFT JOIN product_details d ON d.product_id = c.product_id"
           " WHERE c.product_id IS NOT NULL AND d.product_id IS NULL"
           " ORDER BY c.product_id")
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = [dict(r) for r in db.conn.execute(sql)]
    if not rows:
        print("没有待抓的商品详情。")
        db.close()
        return 0
    print(f"待抓详情 {len(rows)} 个")

    from playwright.sync_api import sync_playwright

    saved = skipped = 0
    started = now_iso()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": cfg.proxy} if cfg.proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=cfg.user_agent, locale="en-GB",
                                  viewport={"width": 1440, "height": 1400})
        page = ctx.new_page()
        for i, r in enumerate(rows, 1):
            try:
                page.goto(r["url"], wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(1800)
                info = page.evaluate(DETAIL_JS) or {}
                body = page.inner_text("body")
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                print(f"  [{i}/{len(rows)}] 失败 {r['product_id']}: {type(exc).__name__}")
                continue

            desc = clean_desc(info.get("desc") or "")
            m = re.search(r"(\d+)\s+components", body, re.I)
            components = int(m.group(1)) if m else None
            if re.search(r"\bpre-?order\b", body, re.I):
                stock = "preorder"
            elif re.search(r"\bout of stock\b", body, re.I):
                stock = "out_of_stock"
            elif re.search(r"\bin stock\b", body, re.I):
                stock = "in_stock"
            else:
                stock = ""
            code = (info.get("code") or "")
            # 页面文本形如 "Product Code: SKU / 99120108074"，只留数字码
            m_code = re.search(r"(\d{9,14})", code)
            code = m_code.group(1) if m_code else ""

            if args.dry_run:
                print(f"  [{i}/{len(rows)}] {r['product_id']:<8} 描述 {len(desc):>4} 字 "
                      f"件数 {components} 库存 {stock} 货号 {code or '—'}")
                continue
            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO product_details"
                    " (product_id, source, url, description, components, stock,"
                    "  product_code, raw_title, fetched_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(product_id) DO UPDATE SET"
                    "   description=excluded.description, components=excluded.components,"
                    "   stock=excluded.stock, product_code=excluded.product_code,"
                    "   raw_title=excluded.raw_title, fetched_at=excluded.fetched_at",
                    (r["product_id"], "element_games", r["url"], desc, components,
                     stock, code, info.get("title") or r["title"], now_iso()),
                )
            saved += 1
            if i % 20 == 0:
                print(f"  进度 {i}/{len(rows)}，已存 {saved}")
            time.sleep(0.5)
        browser.close()

    print(f"\n完成：写入 {saved} 条，失败 {skipped} 条")
    if not args.dry_run:
        db.conn.execute(
            "INSERT INTO price_runs (week, started_at, finished_at, stats, status)"
            " VALUES (?,?,?,?,?)",
            (_iso_week(), started, now_iso(),
             f'{{"crawl_details": {{"saved": {saved}, "skipped": {skipped}}}}}', "ok"),
        )
        db.conn.commit()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
