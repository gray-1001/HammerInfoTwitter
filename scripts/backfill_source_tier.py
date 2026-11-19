# -*- coding: utf-8 -*-
"""S-02 数据回填：把历史上「拿不到发布域名就继承查询源层级」的行改正。

规则与 adapters.GoogleNewsAdapter 保持一致：
- 能匹配 DOMAIN_TIER 的域名 → 按域名定级；
- 拿不到域名 + site: 限定查询 → 沿用该查询配置的层级（发布方是已知的）；
- 拿不到域名 + 普通关键词查询 → L2（不知道是谁发的）。

用法：python scripts/backfill_source_tier.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.adapters import DOMAIN_TIER  # noqa: E402

DB = ROOT / "data" / "infotwitter.db"
SOURCES = ROOT / "config" / "sources.yaml"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    meta = {s["id"]: s for s in cfg.get("sources", []) if s.get("type") == "googlenews"}

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    total_fixed = 0
    print(f"{'源':<22}{'待修正':>8}{'默认层级':>10}")
    print("-" * 42)
    for sid, s in meta.items():
        query = str(s.get("query") or "").lower()
        site_restricted = "site:" in query
        # 拿不到发布域名时该用的层级
        default_tier = s.get("tier", "L2") if site_restricted else "L2"

        rows = con.execute(
            "SELECT id, publisher_domain, derived_tier FROM raw_docs WHERE source_id=?", (sid,)
        ).fetchall()
        fixed = 0
        for r in rows:
            domain = (r["publisher_domain"] or "").strip().lower()
            # 能匹配白名单的域名按域名定级，否则用默认层级
            expected = DOMAIN_TIER.get(domain, default_tier)
            if r["derived_tier"] == expected:
                continue
            fixed += 1
            if not args.dry_run:
                con.execute("UPDATE raw_docs SET derived_tier=? WHERE id=?", (expected, r["id"]))
        if fixed:
            print(f"{sid:<22}{fixed:>8}{default_tier:>10}")
        total_fixed += fixed

    if not args.dry_run:
        con.commit()
    con.close()
    print("-" * 42)
    print(f"合计修正 {total_fixed} 行" + ("（dry-run，未写入）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
