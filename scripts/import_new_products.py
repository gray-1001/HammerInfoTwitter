# -*- coding: utf-8 -*-
"""把目录里**新出现**的商品补进 products（模型 + 小说，排除涂料）。

与原「补书」脚本的区别：
- 只处理 `first_seen_at >= since` 的行，也就是本周新上架的；
- 不限书籍，模型也补（新品多数是模型）；
- 涂料跳过（用户明确不需要涂料价格）。

用法：
    python scripts/import_new_products.py --since-days 7 --dry-run
    python scripts/import_new_products.py --since-days 7
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import CST, Database, now_iso  # noqa: E402
from infotwitter.pricing import classify_category  # noqa: E402

PAINT_HINT = ("ML", "PAINT", "COLOUR", "CONTRAST", "SHADE", "LAYER", "BASE", "SPRAY")

# 书/期刊优先判定：classify_category 会把 JOURNAL TACTICA、规则书判成模型
BOOK_HINT = re.compile(
    r"\b(pb|hb|paperback|hardback|softback|novel|anthology|omnibus|"
    r"rulebook|core book|handbook|journal|codex supplement|black library|"
    r"battletome|attletome)\b", re.I)
# 只收这几类：英文圣典/周边卡牌不进（中文版已有，且没法比价）
KEEP_CATEGORIES = ("mini", "boxed", "terrain", "book")


def guess_line(title: str) -> str:
    low = (title or "").lower()
    if "horus heresy" in low or "siege of terra" in low:
        return "30k"
    if "age of sigmar" in low:
        return "aos"
    if "old world" in low:
        return "tow"
    if "necromunda" in low or "blood bowl" in low:
        return "other"
    return "40k"


def is_paint(title: str) -> bool:
    up = (title or "").upper()
    return any(h in up for h in PAINT_HINT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)
    since = (datetime.now(CST) - timedelta(days=args.since_days)).replace(microsecond=0).isoformat()

    known: set[str] = set()
    for (codes,) in db.conn.execute("SELECT product_codes FROM products"):
        try:
            known.update(str(c) for c in json.loads(codes or "[]"))
        except (TypeError, json.JSONDecodeError):
            continue
    known_ids = {r[0] for r in db.conn.execute("SELECT id FROM products")}

    rows = [dict(r) for r in db.conn.execute(
        "SELECT url, title, price, product_code, first_seen_at, category_path"
        "  FROM overseas_catalog"
        " WHERE first_seen_at >= ? AND price > 0"
        "   AND length(coalesce(product_code,'')) > 0"
        " ORDER BY first_seen_at DESC", (since,)
    )]

    picked, seen = [], set()
    skipped_paint = skipped_known = skipped_cat = 0
    for r in rows:
        code = (r["product_code"] or "").strip()
        title = r["title"] or ""
        if not code.isdigit() or code in known or code in seen:
            skipped_known += 1
            continue
        if is_paint(title):
            skipped_paint += 1
            continue
        # 目录自带的分区是最可靠的品类信号：Black Library / books 区就是书，
        # 比标题规则准（DARK IMPERIUM 这种光看标题会被判成模型）
        path = (r.get("category_path") or "").lower()
        if "blacklibrary" in path or "/books" in path or "book" in path.split("/")[-1]:
            category = "book"
        elif BOOK_HINT.search(title):
            category = "book"
        else:
            category = classify_category(title) or "mini"
        if category not in KEEP_CATEGORIES:
            skipped_cat += 1
            continue
        r["_category"] = category
        seen.add(code)
        picked.append(r)
    if args.limit:
        picked = picked[:args.limit]

    print(f"本周（{since[:10]} 起）目录行 {len(rows)} 条")
    print(f"  已是主表商品/重复码 {skipped_known}，涂料跳过 {skipped_paint}，"
          f"不收的品类（英文圣典/卡牌等）{skipped_cat}")
    print(f"  待补入 {len(picked)} 个")
    for r in picked[:15]:
        print(f"    {r['product_code']:<14} £{r['price']:<8} [{r['_category']:<7}] {r['title'][:50]}")
    if args.dry_run or not picked:
        db.close()
        return 0

    ts = now_iso()
    added = 0
    with db.tx() as conn:
        for r in picked:
            code = r["product_code"].strip()
            if code in known_ids:
                continue
            category = r.get("_category") or "mini"
            conn.execute(
                "INSERT INTO products"
                " (id, name_en, name_zh, barcode, product_codes, product_line,"
                "  category, status, first_seen_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, r["title"], None, None, json.dumps([code], ensure_ascii=False),
                 guess_line(r["title"]), category, "active", ts, ts),
            )
            added += 1
    print(f"\n已补入商品目录 {added} 个")
    left = db.conn.execute("SELECT COUNT(*) FROM products WHERE name_zh IS NULL").fetchone()[0]
    print(f"待翻译 {left} 个")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
