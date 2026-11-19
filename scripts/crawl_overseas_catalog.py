# -*- coding: utf-8 -*-
"""抓海外零售站的全量商品目录（标题 + 价格），落进 overseas_catalog。

为什么不是逐个商品搜索：Element Games 的**子分类页**一页就是 40–100 个商品带价格，
100 来个分类页就能覆盖几千个 SKU；逐个搜索是 1745 次页面加载，完全不是一个量级。

用法：
    python scripts/crawl_overseas_catalog.py --limit-pages 5     # 先试 5 个分类页
    python scripts/crawl_overseas_catalog.py                     # 全部
    python scripts/crawl_overseas_catalog.py --dry-run            # 只打印不写库
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import Database, now_iso  # noqa: E402
from infotwitter.pricing.importers import _iso_week  # noqa: E402

BASE = "https://elementgames.co.uk"
LANDINGS = [
    "https://elementgames.co.uk/games-workshop/warhammer-40k",
    "https://elementgames.co.uk/games-workshop/new-warhammer-age-of-sigmar",
    "https://elementgames.co.uk/games-workshop/the-horus-heresy",
    "https://elementgames.co.uk/games-workshop/necromunda",
    "https://elementgames.co.uk/games-workshop/blood-bowl",
    "https://elementgames.co.uk/games-workshop/kill-team",
    # 小说（Black Library）不在上面几个系统分类里，需要单独一个入口。
    # 这个落地页本身就列商品，所以下面会顺带抽入口页自己的卡片。
    "https://elementgames.co.uk/games-workshop/BlackLibrary",
]

# 抓「商品卡片」：页面上带产品链接、且卡片里有 £ 价格
EXTRACT_JS = """() => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('a[href]').forEach(a => {
        const href = a.href.split('?')[0].split('#')[0];
        if (!href.includes('elementgames.co.uk')) return;
        if (href.split('/').length < 5) return;
        const card = a.closest('li, article, .product, [class*="product"]') || a.parentElement;
        if (!card) return;
        const text = (card.innerText || '').trim();
        const m = text.match(/£\\s*([\\d,]+\\.\\d{2})/);
        if (!m) return;
        const name = (a.innerText || '').trim().split('\\n')[0];
        if (!name || name.length < 5) return;
        const key = href;
        if (seen.has(key)) return;
        seen.add(key);
        out.push({href, name: name.replace(/\\s+/g, ' ').slice(0, 120),
                  price: parseFloat(m[1].replace(/,/g, ''))});
    });
    return out;
}"""

SUBCAT_JS = """() => [...document.querySelectorAll('a[href]')]
    .map(a => a.href.split('?')[0].split('#')[0])
    .filter(h => h.includes('elementgames.co.uk/games-workshop/'))"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-pages", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-landing", default="", help="只抓某个入口（URL 片段匹配）")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)
    from playwright.sync_api import sync_playwright

    landings = [u for u in LANDINGS if not args.only_landing or args.only_landing in u]
    started = now_iso()
    saved = 0
    pages_done = 0


    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": cfg.proxy} if cfg.proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=cfg.user_agent, locale="en-GB",
                                  viewport={"width": 1440, "height": 1600})
        page = ctx.new_page()

        # 1) 从入口页收集子分类，同时抽入口页自己的商品卡片
        #    （Black Library 这类落地页是直接列商品的，不抽会漏掉小说）
        subcats: list[str] = []
        landing_fail = 0
        for landing in landings:
            try:
                page.goto(landing, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1500)
                links = page.evaluate(SUBCAT_JS) or []
                landing_items = page.evaluate(EXTRACT_JS) or []
            except Exception as exc:  # noqa: BLE001
                landing_fail += 1
                print(f"  入口失败 {landing}: {type(exc).__name__}")
                continue
            for h in links:
                # 只要该入口下的二级分类（比入口多一层）
                if h.count("/") == landing.count("/") + 1 and h not in subcats:
                    subcats.append(h)
            if landing_items:
                pages_done += 1
                if not args.dry_run:
                    ts = now_iso()
                    with db.tx() as conn:
                        for it in landing_items:
                            conn.execute(
                                "INSERT INTO overseas_catalog"
                                " (url, source, title, price, currency, category_path, first_seen_at, crawled_at)"
                                " VALUES (?,?,?,?,?,?,?,?)"
                                " ON CONFLICT(url) DO UPDATE SET"
                                "   title=excluded.title, price=excluded.price,"
                                "   category_path=excluded.category_path, crawled_at=excluded.crawled_at",
                                (it["href"], "element_games", it["name"], it["price"],
                                 "GBP", urlparse(landing).path.strip("/"), ts, ts),
                            )
                            saved += 1
            print(f"  入口 {landing.rsplit('/', 1)[-1]}：发现 {len(links)} 个链接，"
                  f"页面自带 {len(landing_items)} 个商品")

        print(f"\n共 {len(subcats)} 个子分类页")
        if landing_fail and landing_fail == len(landings):
            # 一个入口都打不开 = 站点不可用，别拿着空结果继续写库
            print("所有入口都失败，判定站点不可用，本次不写库")
            browser.close()
            db.close()
            return 2
        if args.limit_pages:
            subcats = subcats[:args.limit_pages]

        # 2) 逐个分类页抽商品
        for i, url in enumerate(subcats, 1):
            path = urlparse(url).path.strip("/")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(2000)
                items = page.evaluate(EXTRACT_JS) or []
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}/{len(subcats)}] 失败 {path}: {type(exc).__name__}")
                continue
            pages_done += 1
            if not args.dry_run and items:
                ts = now_iso()
                with db.tx() as conn:
                    for it in items:
                        conn.execute(
                            "INSERT INTO overseas_catalog"
                            " (url, source, title, price, currency, category_path, first_seen_at, crawled_at)"
                            " VALUES (?,?,?,?,?,?,?,?)"
                            " ON CONFLICT(url) DO UPDATE SET"
                            "   title=excluded.title, price=excluded.price,"
                            "   category_path=excluded.category_path, crawled_at=excluded.crawled_at",
                            (it["href"], "element_games", it["name"], it["price"],
                             "GBP", path, ts, ts),
                        )
                        saved += 1
            print(f"  [{i}/{len(subcats)}] {path:<52} {len(items):>3} 个商品")
            time.sleep(0.8)
        browser.close()

    total = db.conn.execute("SELECT COUNT(*) c FROM overseas_catalog").fetchone()["c"]
    priced = db.conn.execute(
        "SELECT COUNT(*) c FROM overseas_catalog WHERE price IS NOT NULL").fetchone()["c"]
    print(f"\n本次处理 {pages_done} 个分类页，写入/更新 {saved} 条")
    print(f"目录累计 {total} 条，其中有价格 {priced} 条")
    if not args.dry_run:
        db.conn.execute(
            "INSERT INTO price_runs (week, started_at, finished_at, stats, status)"
            " VALUES (?,?,?,?,?)",
            (_iso_week(), started, now_iso(),
             f'{{"crawl_catalog": {{"pages": {pages_done}, "saved": {saved}}}}}', "ok"),
        )
        db.conn.commit()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
