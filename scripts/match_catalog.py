# -*- coding: utf-8 -*-
"""把海外目录（overseas_catalog）匹配回 GW 货号，并写入价格点。

为什么单独一层：抓取只负责"把商品和价格拿回来"，匹配是另一件事，混在一起时
一旦匹配规则改了就得重抓。这里目录是事实，匹配是可重算的推断。

用法：
    python scripts/match_catalog.py --dry-run          # 只看匹配结果
    python scripts/match_catalog.py                    # 回填 product_id + 写价格点
    python scripts/match_catalog.py --min-score 0.8    # 提高门槛
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import Database, now_iso  # noqa: E402
from infotwitter.pricing.overseas import _similarity, _tokens  # noqa: E402

# 变体特征：6 件装 / 容量 / 赛季 / 期号 / 语言版本
# 注意要覆盖三种写法：6-PACK / 6 PACK / 6PK（零售站常用缩写）
_PACK = re.compile(
    r"(?:(\d+)\s*[- ]?pack\b)|(?:(\d+)\s*pk\b)|(?:x\s*(\d+)\b)|(?:row\s*x?\s*(\d+))", re.I)
_ML = re.compile(r"(\d+)\s*ml\b", re.I)
_SEASON = re.compile(r"season\s*(\d+)", re.I)
_ISSUE = re.compile(r"issue\s*(\d+)", re.I)
_EDITION = re.compile(r"\b(chinese|chn|japanese|jpn|english|eng)\b", re.I)

# 跨产品线的标记：HH 的树脂线和 40k 塑料线常有同名商品，货号完全不同
_RANGE_MARKERS = frozenset({"legio", "legion", "heresy", "horus", "30k", "hh"})


def prefix_conflict(a: str, b: str) -> bool:
    """两个名字的产品线前缀是不是"不同产品线"。

    `LEGIO CUSTODES: SHIELD CAPTAIN`（荷鲁斯之乱树脂线，99123043018）与
    `CUSTODES: SHIELD CAPTAIN`（40k 塑料线，99123043004）名字只差一个词，
    但确实是两个商品——用货号核对时抓到了 16 条这种错配。
    """
    pa = _tokens(a.split(":")[0]) if ":" in a else set()
    pb = _tokens(b.split(":")[0]) if ":" in b else set()
    if not pa or not pb or pa == pb:
        return False
    return bool((pa ^ pb) & _RANGE_MARKERS)


def variant_signature(text: str) -> frozenset[tuple[str, str]]:
    """把规格抽成可比较的签名。

    必须从**原始文本**里抽，不能从 `_tokens` 来的词里抽——`_tokens` 会丢掉纯数字，
    而 SEASON 2 / ISSUE 21 / 6-PACK 的关键信息全在数字上（这是价格模块审计里
    「变体不进签名」那条 P1 的直接后果）。
    """
    low = (text or "").lower()
    sig: set[tuple[str, str]] = set()
    for m in _PACK.finditer(low):
        n = next((g for g in m.groups() if g), None)
        if n:
            sig.add(("pack", str(int(n))))
    for m in _ML.finditer(low):
        sig.add(("ml", str(int(m.group(1)))))
    for m in _SEASON.finditer(low):
        sig.add(("season", m.group(1)))
    for m in _ISSUE.finditer(low):
        sig.add(("issue", m.group(1)))
    for m in _EDITION.finditer(low):
        sig.add(("lang", m.group(1).lower()))
    return frozenset(sig)


def lang_ok(a: frozenset, b: frozenset) -> bool:
    """语言只在双方都标注且不同时才冲突。

    海外零售站通常不写 (ENGLISH)——英文市场默认就是英文；如果强制要求签名相等，
    所有英文版商品都会被拒（审计实测 10 个商品 100% 拒配）。
    """
    la = {v for k, v in a if k == "lang"}
    lb = {v for k, v in b if k == "lang"}
    if not la or not lb:
        return True
    return bool(la & lb)


def spec_ok(a: frozenset, b: frozenset) -> bool:
    """pack / ml / season / issue 必须完全一致。"""
    sa = {(k, v) for k, v in a if k != "lang"}
    sb = {(k, v) for k, v in b if k != "lang"}
    return sa == sb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=float, default=0.75)
    ap.add_argument("--write-points", action="store_true", help="同时写 price_points(gw_uk)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)

    products = [dict(r) for r in db.conn.execute(
        "SELECT id, name_en, product_codes, category FROM products ORDER BY id")]
    if args.limit:
        products = products[:args.limit]
    catalog = [dict(r) for r in db.conn.execute(
        "SELECT url, title, price, category_path, product_code FROM overseas_catalog")]

    name_min = max(args.min_score, 0.9)   # 无货号背书时的名称匹配门槛
    matched: list[tuple[str, str, float]] = []
    low_conf = 0
    no_cand = 0
    used: dict[str, str] = {}   # catalog url -> product_id，防止一物多配

    # ---------- 第一层：按 GW 官方货号精确匹配（最强证据，不用猜名字） ----------
    code_to_pid: dict[str, str] = {}
    for p in products:
        try:
            codes = json.loads(p["product_codes"] or "[]")
        except (TypeError, json.JSONDecodeError):
            codes = []
        for code in codes:
            code_to_pid[str(code).strip()] = p["id"]
    by_code = 0
    for row in catalog:
        code = (row.get("product_code") or "").strip()
        pid = code_to_pid.get(code) if code else None
        if not pid or row["url"] in used or pid in used.values():
            continue
        if not row.get("price") or float(row["price"]) <= 0:
            continue          # 预售价 / 未定价，不能当观测值
        used[row["url"]] = pid
        matched.append((pid, row["url"], 1.0))
        by_code += 1
    print(f"按货号匹配 {by_code} 条")

    # ---------- 第二层：按名称 + 变体签名匹配（没有货号的走这里） ----------
    # 倒排索引：词 -> 目录行号，避免 1745 × N 的全量比对
    index: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(catalog):
        if row["url"] in used:
            continue
        for t in _tokens(row["title"]):
            index[t].append(i)

    for p in products:
        if p["id"] in used.values():
            continue
        try:
            p_codes = {str(c) for c in json.loads(p["product_codes"] or "[]")}
        except (TypeError, json.JSONDecodeError):
            p_codes = set()
        toks = _tokens(p["name_en"])
        sig = variant_signature(p["name_en"])
        cand: set[int] = set()
        for t in toks:
            cand.update(index.get(t, ()))
        if not cand:
            no_cand += 1
            continue
        best_i, best_s = -1, 0.0
        for i in cand:
            if catalog[i]["url"] in used:
                continue
            # 货号不一致 → 名字再像也不是同一个商品（货号是权威证据）
            ccode = (catalog[i].get("product_code") or "").strip()
            if ccode.isdigit() and ccode not in p_codes:
                continue
            if prefix_conflict(p["name_en"], catalog[i]["title"]):
                continue
            if not spec_ok(sig, variant_signature(catalog[i]["title"])):
                continue
            if not lang_ok(sig, variant_signature(catalog[i]["title"])):
                continue
            s = _similarity(p["name_en"], catalog[i]["title"])
            if s > best_s:
                best_i, best_s = i, s
        if best_i < 0 or best_s < name_min:
            if best_s >= 0.6:
                low_conf += 1
            continue
        # 名称匹配没有货号背书，只接受高分且价格有效的
        if not catalog[best_i].get("price") or float(catalog[best_i]["price"]) <= 0:
            continue
        used[catalog[best_i]["url"]] = p["id"]
        matched.append((p["id"], catalog[best_i]["url"], round(best_s, 3)))

    print(f"商品 {len(products)} 个，目录 {len(catalog)} 条")
    print(f"匹配成功 {len(matched)} 个（阈值 {args.min_score}），"
          f"有候选但分不够 {low_conf}，无候选 {no_cand}")

    from collections import Counter
    print("\n按目录分类看命中：")
    hit_by_path = Counter()
    for pid, url, _ in matched:
        row = next(c for c in catalog if c["url"] == url)
        hit_by_path[row["category_path"].split("/")[-1]] += 1
    for k, v in hit_by_path.most_common(10):
        print(f"  {v:>4}  {k}")

    if matched:
        print("\n样例（前 12）：")
        for pid, url, s in matched[:12]:
            row = next(c for c in catalog if c["url"] == url)
            p = next(x for x in products if x["id"] == pid)
            print(f"  {s:.2f} {pid:<8} {p['name_en'][:38]:<40}")
            print(f"        ↔ £{row['price']:<8} {row['title'][:56]}")

    if args.dry_run:
        db.close()
        return 0

    ts = now_iso()
    with db.tx() as conn:
        for pid, url, s in matched:
            conn.execute(
                "UPDATE overseas_catalog SET product_id=?, match_score=? WHERE url=?",
                (pid, s, url),
            )
        if args.write_points:
            for pid, url, s in matched:
                row = next(c for c in catalog if c["url"] == url)
                verified = (row.get("product_code") or "").strip().isdigit()
                conf = "high" if verified else "medium"
                conn.execute(
                    "INSERT INTO price_points"
                    " (product_id, channel, platform, price, currency, price_cny,"
                    "  url, observed_at, confidence, note)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(product_id, channel, platform, observed_at) DO UPDATE SET"
                    "   price=excluded.price, price_cny=excluded.price_cny,"
                    "   confidence=excluded.confidence, note=excluded.note",
                    (pid, "gw_uk", "Element Games", row["price"], "GBP",
                     round(row["price"] * float(cfg.get("pricing.rates.GBP_CNY", 9.2)), 2),
                     url, ts[:10], conf,
                     f"目录匹配 {s:.2f}「{row['title'][:40]}」"),
                )
    print(f"\n已回填 {len(matched)} 条匹配"
          + (f"，写入 {len(matched)} 个价格点" if args.write_points else ""))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
