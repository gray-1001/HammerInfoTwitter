# -*- coding: utf-8 -*-
"""每周新品：抓目录 → 补货号 → 补进商品目录 → 翻译 → 匹配写价。

为什么要单独一个编排：这五步以前都是手动按顺序跑的，任何一步漏掉都会让
"新品"进不了价格体系（比如只抓了目录不补货号，就永远匹配不上）。

用法：
    python scripts/weekly_price_update.py                # 完整一轮
    python scripts/weekly_price_update.py --skip-crawl    # 跳过抓取，用库里现有数据
    python scripts/weekly_price_update.py --dry-run       # 只打印每步会做什么
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import CST, Database, now_iso  # noqa: E402
from infotwitter.pricing.importers import _iso_week  # noqa: E402

PY = sys.executable
SCRIPTS = ROOT / "scripts"


def run(script: str, *args: str) -> int:
    """跑一个子脚本，输出直接透传。失败不抛异常，返回退出码由调用方决定。"""
    cmd = [PY, str(SCRIPTS / script), *args]
    print(f"\n$ {' '.join([Path(script).name, *args])}", flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT))
    print(f"  → 退出码 {proc.returncode}，用时 {time.time() - t0:.0f}s", flush=True)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-crawl", action="store_true")
    ap.add_argument("--days", type=int, default=7, help="把多少天内新出现的目录行当作新品")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--crawl-pages", type=int, default=0, help="只抓前 N 个分类页（0=全部）")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)
    since = (datetime.now(CST) - timedelta(days=args.days)).replace(microsecond=0).isoformat()

    before = {
        "catalog": db.conn.execute("SELECT COUNT(*) FROM overseas_catalog").fetchone()[0],
        "products": db.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "priced": db.conn.execute(
            "SELECT COUNT(DISTINCT product_id) FROM price_points WHERE channel='gw_uk'"
        ).fetchone()[0],
    }
    stats = {"started": now_iso(), "since": since, "before": before, "steps": {}}

    print(f"=== 每周新品更新（{since[:10]} 之后）===")
    print(f"起始：目录 {before['catalog']} 条 / 商品 {before['products']} 个 / 有海外价 {before['priced']} 个")

    if args.dry_run:
        new_rows = db.conn.execute(
            "SELECT COUNT(*) FROM overseas_catalog WHERE first_seen_at >= ?", (since,)
        ).fetchone()[0]
        missing_code = db.conn.execute(
            "SELECT COUNT(*) FROM overseas_catalog WHERE first_seen_at >= ?"
            " AND length(coalesce(product_code,''))=0 AND price>0", (since,)
        ).fetchone()[0]
        print(f"\n[dry-run] 本周新出现的目录行 {new_rows} 条，其中待补货号 {missing_code} 条")
        db.close()
        return 0

    # 1) 抓目录（整站分类页）。抓不到（站点宕机/限流）就停在这里，不要带着旧数据硬跑
    if not args.skip_crawl:
        crawl_args = ["--limit-pages", str(args.crawl_pages)] if args.crawl_pages else []
        rc = run("crawl_overseas_catalog.py", *crawl_args)
        stats["steps"]["crawl"] = rc
        if rc != 0:
            # 抓取失败（站点宕机/限流）就不要继续：只用旧数据跑一遍会假装“本周无新品”
            print("  ⚠ 抓取失败，本轮中止（避免用旧数据冒充新品）")
            db.close()
            return rc

    # 2) 给本周新增的目录行补货号
    rc = run("fill_catalog_codes.py", "--limit", "400", "--since-days", str(args.days))
    stats["steps"]["codes"] = rc

    # 3) 把有货号、主表没有的新品补进商品目录（涂料按用户要求排除）
    rc = run("import_new_products.py", "--since-days", str(args.days))
    stats["steps"]["import"] = rc

    # 4) 翻译新商品
    rc = run("translate_products.py", "--batch", "20", "--workers", "2")
    stats["steps"]["translate"] = rc

    # 5) 匹配 + 写价格点
    rc = run("match_catalog.py", "--write-points")
    stats["steps"]["match"] = rc

    after = {
        "catalog": db.conn.execute("SELECT COUNT(*) FROM overseas_catalog").fetchone()[0],
        "products": db.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "priced": db.conn.execute(
            "SELECT COUNT(DISTINCT product_id) FROM price_points WHERE channel='gw_uk'"
        ).fetchone()[0],
    }
    stats["after"] = after
    stats["finished"] = now_iso()
    delta = {k: after[k] - before[k] for k in after}

    print("\n=== 本轮结果 ===")
    print(f"  目录   {before['catalog']:>6} → {after['catalog']:>6}（{delta['catalog']:+}）")
    print(f"  商品   {before['products']:>6} → {after['products']:>6}（{delta['products']:+}）")
    print(f"  有价   {before['priced']:>6} → {after['priced']:>6}（{delta['priced']:+}）")

    # 各步退出码必须影响整轮结果：以前只记录不判断，第 2-5 步失败也返回 0，
    # 计划任务看到的是「成功」，周更静默失败没人知道（P-11）。
    failed_steps = {k: v for k, v in stats["steps"].items() if v != 0}
    print("\n=== 各步退出码 ===")
    for k, v in stats["steps"].items():
        print(f"  {k:<10} {'OK' if v == 0 else f'失败({v})'}")

    status = "ok" if not failed_steps else "failed"
    db.conn.execute(
        "INSERT INTO price_runs (week, started_at, finished_at, stats, status) VALUES (?,?,?,?,?)",
        (_iso_week(), stats["started"], stats["finished"],
         __import__("json").dumps({"weekly": stats}, ensure_ascii=False), status),
    )
    db.conn.commit()
    db.close()
    if failed_steps:
        print(f"\n⚠ 有 {len(failed_steps)} 个步骤失败：{failed_steps}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
