# -*- coding: utf-8 -*-
"""把海外目录里、主表没有的书/小说 SKU 补进 products。

背景：主表来自 GW 中国调价表，只含 7 本规则书，**一本小说都没有**；
而 Element 的 Black Library 区有几十本小说，且都带 GW 官方码。
不补进主表，这些书就永远进不了价格体系。

用法：
    python scripts/import_catalog_books.py --dry-run
    python scripts/import_catalog_books.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import Database, now_iso  # noqa: E402
from infotwitter.pricing import classify_category  # noqa: E402

# 书籍/小说特征（宁可少收，不要把模型当书）
BOOK_RE = re.compile(
    r"\b(pb|hb|paperback|hardback|softback|novel|anthology|omnibus|"
    r"black library|audio ?book|limited edition)\b", re.I)
# 明确不是书的
NOT_BOOK = re.compile(r"\b(codex|battletome|rulebook|cards?|deck|dice|"
                      r"miniatures?|squad|team|tank|battalion)\b", re.I)


def book_line(title: str) -> str:
    low = title.lower()
    if "horus heresy" in low or "siege of terra" in low:
        return "30k"
    if "age of sigmar" in low:
        return "aos"
    if "necromunda" in low:
        return "other"
    return "40k"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)

    # 主表已有的官方码
    known: set[str] = set()
    for (codes,) in db.conn.execute("SELECT product_codes FROM products"):
        try:
            known.update(str(c) for c in json.loads(codes or "[]"))
        except (TypeError, json.JSONDecodeError):
            continue
    known_ids = {r[0] for r in db.conn.execute("SELECT id FROM products")}

    rows = [dict(r) for r in db.conn.execute(
        "SELECT url, title, price, product_code FROM overseas_catalog"
        " WHERE price > 0 AND length(coalesce(product_code,'')) > 0"
        " ORDER BY title")]
    picked, seen = [], set()
    for r in rows:
        code = r["product_code"].strip()
        title = r["title"] or ""
        if not code.isdigit() or code in known or code in seen:
            continue
        if not BOOK_RE.search(title) or NOT_BOOK.search(title):
            continue
        seen.add(code)
        picked.append(r)
    if args.limit:
        picked = picked[:args.limit]

    print(f"待补入主表的书籍 {len(picked)} 个")
    for r in picked[:12]:
        print(f"  {r['product_code']:<14} £{r['price']:<8} {r['title'][:58]}")
    if args.dry_run:
        db.close()
        return 0

    ts = now_iso()
    added = 0
    with db.tx() as conn:
        for r in picked:
            code = r["product_code"].strip()
            if code in known_ids:      # id 撞了就跳过，不覆盖已有商品
                continue
            conn.execute(
                "INSERT INTO products"
                " (id, name_en, name_zh, barcode, product_codes, product_line,"
                "  category, status, first_seen_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (code, r["title"], None, None, json.dumps([code], ensure_ascii=False),
                 book_line(r["title"]), "book", "active", ts, ts),
            )
            added += 1
    print(f"已补入 {added} 个")
    left = db.conn.execute("SELECT COUNT(*) FROM products WHERE name_zh IS NULL").fetchone()[0]
    print(f"待翻译 {left} 个（跑 scripts/translate_products.py）")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
