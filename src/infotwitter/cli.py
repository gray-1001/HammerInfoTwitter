"""命令行入口。"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .adapters import collect
from .config import load_config
from .db import Database, now_iso
from .runner import run_daily

logger = logging.getLogger("infotwitter")


def _iso_week() -> str:
    """ISO 周编号，如 2026-W47（price_runs 用它做进度台账）。"""
    import datetime as _dt

    year, week, _ = _dt.date.today().isocalendar()
    return f"{year}-W{week:02d}"


def setup_logging(log_dir: Path, level: str = "INFO", keep_days: int = 30) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    today = __import__("datetime").date.today()
    file_handler = logging.FileHandler(
        log_dir / f"run-{today.isoformat()}.log", encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    _prune_old_logs(log_dir, keep_days)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _prune_old_logs(log_dir: Path, keep_days: int) -> None:
    """日志保留 N 天（设计 §12），否则长跑后 logs/ 无界增长。"""
    import datetime as _dt

    cutoff = _dt.date.today() - _dt.timedelta(days=max(1, keep_days))
    for path in log_dir.glob("run-*.log"):
        token = path.stem.replace("run-", "")
        try:
            if _dt.date.fromisoformat(token) < cutoff:
                path.unlink(missing_ok=True)
        except ValueError:
            continue


def cmd_collect(args: argparse.Namespace, cfg) -> int:
    db = Database(cfg.db_path)
    db.upsert_sources(cfg.sources)
    docs, failed = collect(cfg, db)
    inserted = db.insert_raw_docs(docs)
    print(f"采集 {len(docs)} 条，新增 {inserted} 条，失败源 {len(failed)} 个")
    if failed:
        print("失败：", ", ".join(failed))
    db.close()
    return 0


def cmd_run(args: argparse.Namespace, cfg) -> int:
    stats = run_daily(
        cfg,
        dry_run=bool(args.dry_run),
        skip_push=bool(args.no_push),
        force=bool(args.force),
        reanalyze=bool(args.reanalyze),
        skip_collect=bool(args.no_collect),
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("status") == "locked":
        # 锁冲突不是"正常结束"：返回非 0，让任务计划程序的重试与告警能看见（P1-6）
        return 4
    push = stats.get("push")
    if isinstance(push, dict) and push.get("ok") is False:
        # 失败必须体现在退出码上，否则任务计划程序永远显示成功（核查报告 BUG-15）
        return 3
    return 0


def cmd_status(args: argparse.Namespace, cfg) -> int:
    db = Database(cfg.db_path)
    today = __import__("datetime").datetime.now().date().isoformat()
    rows = db.pushes_on(today)
    print(f"数据库：{cfg.db_path}")
    print(f"今日推送记录：{len(rows)} 条")
    for r in rows:
        # delivery_ok: 1=已送达 0=失败 2=在途（进程在发送与记账之间被杀）
        state = {1: "OK", 0: "FAIL", 2: "在途"}.get(r["delivery_ok"], str(r["delivery_ok"]))
        print(f"  槽{r['slot']}  {r['title']}  {r['importance']}  {state}")
    n_story = db.conn.execute(
        "SELECT status, COUNT(*) c FROM stories GROUP BY status"
    ).fetchall()
    print("\n事件状态：")
    for r in n_story:
        print(f"  {r['status']:<10} {r['c']}")
    n_raw = db.conn.execute("SELECT COUNT(*) c FROM raw_docs").fetchone()["c"]
    n_ana = db.conn.execute("SELECT COUNT(*) c FROM raw_docs WHERE analyzed_at IS NOT NULL").fetchone()["c"]
    print(f"\n原始文档：{n_raw} 条，已分析 {n_ana} 条")
    db.close()
    return 0


def cmd_doctor(args: argparse.Namespace, cfg) -> int:
    from .doctor import FAIL, run_checks, render

    checks = run_checks(cfg, probe_push=bool(getattr(args, "probe_push", False)))
    print(render(checks))
    # 所有检查项都要参与判定：只取前 5 项会让 DB / 源健康 / 定时任务的 FAIL 静默通过
    return 0 if not any(c.status == FAIL for c in checks) else 1


def cmd_sources(args: argparse.Namespace, cfg) -> int:
    """按源查看最近 N 天的产出：条数、失败次数、平均正文长度。"""
    from .db import Database

    db = Database(cfg.db_path)
    try:
        rows = db.source_stats_summary(int(args.days))
        if not rows:
            print(f"最近 {args.days} 天没有源统计（跑一次 collect 或 run 就会写入）")
            return 0
        print(f"最近 {args.days} 天按源产出（按条数排序）")
        print(f"{'源':<22}{'条数':>6}{'失败':>6}{'均正文':>8}{'活跃天':>7}")
        print("-" * 52)
        for r in rows:
            print(
                f"{r['source_id']:<22}{r['docs'] or 0:>6}{r['fails'] or 0:>6}"
                f"{r['avg_body'] or 0:>8}{r['days'] or 0:>7}"
            )
        total = sum(r["docs"] or 0 for r in rows)
        print("-" * 52)
        print(f"合计 {total} 条，来自 {len(rows)} 个源")
    finally:
        db.close()
    return 0


def cmd_login(args: argparse.Namespace, cfg) -> int:
    """登录需要账号态的站点（目前：nga）。"""
    site = (args.site or "").strip().lower()
    if site == "nga":
        from .adapters.nga import login as nga_login
        ok = nga_login(wait_seconds=int(args.wait), proxy=cfg.proxy, user_agent=cfg.user_agent)
        return 0 if ok else 1
    print(f"未支持的站点：{site}（可选：nga）")
    return 2


def cmd_price(args: argparse.Namespace, cfg) -> int:
    from pathlib import Path as _Path
    from .db import Database
    from .price_sources import SITES, login as cn_login, median_price, search as cn_search
    from .pricing import (
        build_summary, fetch_element_games, import_gw_excel, import_manual_csv,
        latest_price_date, record_price_run, render_summary, save_overseas_points,
    )

    db = Database(cfg.db_path)
    try:
        if args.login:
            site = args.login
            if site not in SITES:
                print(f"未知站点 {site}，可选：{', '.join(SITES)}")
                return 2
            ok = cn_login(site, cfg.proxy, cfg.user_agent, wait_seconds=int(args.wait))
            return 0 if ok else 1

        if args.fetch_cn:
            sites = [s.strip() for s in str(args.fetch_cn).split(",") if s.strip()]
            for s in sites:
                if s not in SITES:
                    print(f"未知站点 {s}，可选：{', '.join(SITES)}")
                    return 2

            if args.translate:
                from .pricing import translate_names
                translate_names(db.conn, cfg, limit=int(args.limit))

            sql = (
                "SELECT id, name_en, name_zh FROM products WHERE name_zh IS NOT NULL AND name_zh != ''"
                + (" AND product_line=?" if args.line else "")
                + " ORDER BY id LIMIT ?"
            )
            rows = db.conn.execute(
                sql, ([args.line] if args.line else []) + [int(args.limit)]
            ).fetchall()
            if not rows:
                print("没有中文品名可搜，先跑 --translate 生成")
                return 1

            from .pricing import is_digital_goods, search_queries, select_relevant
            print(f"准备为 {len(rows)} 个商品抓取国内价（站点：{', '.join(sites)}）")
            saved = 0
            for site in sites:
                for r in rows:
                    official_row = db.conn.execute(
                        "SELECT price_cny FROM price_points WHERE product_id=? AND channel='cn_official' "
                        "ORDER BY observed_at DESC LIMIT 1", (r["id"],)
                    ).fetchone()
                    official = official_row["price_cny"] if official_row else None

                    # 先精确后放宽：完整译名最准，但常常搜不到（闲鱼会回退成推荐流）；
                    # 只用宽词又会让同阵营不同单位拿到同一批货、同一个价
                    keyword = ""
                    raw_items: list = []
                    items: list = []
                    reason = ""
                    used_fallback = False
                    queries = search_queries(r["name_zh"])
                    for idx, kw in enumerate(queries):
                        keyword = kw
                        raw_items = cn_search(
                            site, cfg, kw, limit=int(args.samples) * 3,
                            headless=not args.show,
                            require=[t for t in kw.split() if t],
                        )
                        pool = [i for i in raw_items if not is_digital_goods(i["title"])]
                        items, reason = select_relevant(pool, kw, official)
                        if len(items) >= 3:
                            used_fallback = idx > 0
                            break
                    med = median_price(items)
                    if not med:
                        print(f"  {r['id']:<8} {keyword[:18]:<20} 无有效结果（原始 {len(raw_items)} 条，{reason}）")
                        continue
                    lo = items[0]
                    # 置信度跟着样本量走：3 条闲鱼挂牌不等于"已核实"，
                    # 以前 3 条就标 high，让整行看起来像官方确认过（价格准确性）
                    if len(items) >= 8:
                        confidence = "high"
                    elif len(items) >= 3:
                        confidence = "medium"
                    else:
                        confidence = "low"
                    # 退到宽词才搜到的，只能代表「这个阵营」的行情，不能当这个型号的价
                    if used_fallback and confidence == "high":
                        confidence = "medium"
                    db.conn.execute(
                        "INSERT OR REPLACE INTO price_points "
                        "(product_id, channel, platform, price, currency, price_cny, url, observed_at, confidence, note) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], site, SITES[site]["name"], med, "CNY", med, lo["url"],
                         now_iso()[:10], confidence,
                         f"中位价，样本 {len(items)} 条，{reason}；最低 ¥{lo['price']:.0f}「{lo['title'][:24]}」"),
                    )
                    db.conn.commit()
                    saved += 1
                    print(
                        f"  {r['id']:<8} {keyword[:18]:<20} 中位 ¥{med:>6.0f}  "
                        f"（{len(items)} 条，最低 ¥{lo['price']:.0f}）"
                    )
            print(f"\n入库 {saved} 条国内价")
            return 0

        if args.fetch_overseas:
            line = args.line
            # 中文版商品（CHINESE/CHN）不要抓：Element Games 卖的是英文版，
            # 抓回来也必然被版本守卫拒掉，白跑一遍浏览器（价格模块有效性）
            sql = ("SELECT id, name_en FROM products"
                   " WHERE category NOT IN ('paint','terrain','accessory')"
                   "   AND name_en NOT LIKE '%(CHINESE)%'"
                   "   AND name_en NOT LIKE '%CHINESE%'"
                   "   AND name_en NOT LIKE '%(CHN)%'"
                   "   AND name_en NOT LIKE '% CHN'")
            params: list = []
            if line:
                sql += " AND product_line=?"
                params.append(line)
            # 抓取策略（两段式）：
            #   A. 重抓段：已经有海外价、但观测最旧的商品 —— 只有重复观测同一商品
            #      才能算出涨跌，纯轮换会让每个商品永远只有一天数据；
            #   B. 扩展段：还没抓过的商品，按周轮换 —— 原来固定 ORDER BY updated_at，
            #      每天都是同一批前 N 个，1745 个里实际只试过 6 个。
            import datetime as _dt

            limit = max(1, int(args.limit))
            focus_n = max(1, limit // 2)
            week = _dt.date.today().isocalendar()
            seed = int(f"{week[0]}{week[1]:02d}")

            focus_sql = (
                "SELECT p.id, p.name_en, MIN(pp.observed_at) AS last_seen FROM products p"
                " JOIN price_points pp ON pp.product_id = p.id AND pp.channel='gw_uk'"
                " WHERE 1=1" + (" AND p.product_line=?" if line else "")
                + " GROUP BY p.id, p.name_en ORDER BY last_seen ASC LIMIT ?"
            )
            focus_params: list = ([line] if line else []) + [focus_n]
            focus_rows = [(r["id"], r["name_en"]) for r in db.conn.execute(focus_sql, focus_params)]

            rest_sql = sql + (
                " AND products.id NOT IN (SELECT DISTINCT product_id FROM price_points"
                "   WHERE channel='gw_uk')"
                " ORDER BY (ABS(products.rowid * 2654435761 + ?) % 100003) LIMIT ?"
            )
            rest_rows = [
                (r["id"], r["name_en"])
                for r in db.conn.execute(rest_sql, [*params, seed, limit - len(focus_rows)])
            ]
            items = focus_rows + rest_rows
            skipped_cn = db.conn.execute(
                "SELECT COUNT(*) c FROM products WHERE name_en LIKE '%CHINESE%'"
                " OR name_en LIKE '%CHN%'"
            ).fetchone()["c"]
            print(f"准备抓取 {len(items)} 个商品的海外价（已跳过 {skipped_cn} 个中文版商品）…")
            results = fetch_element_games(cfg, items, max_items=len(items), min_score=float(args.min_score))
            saved = save_overseas_points(db.conn, cfg, results)
            for pid, d in results.items():
                print(f"  {pid}  £{d['price']:.2f}  匹配度 {d['score']:.2f}  <- {(d['matched'] or '')[:46]}")
            print(f"\n成功 {len(results)}/{len(items)}，入库 {saved} 条")
            record_price_run(db.conn, _iso_week(),
                             {"fetch_overseas": {"tried": len(items), "matched": len(results),
                                                 "saved": saved}})
            return 0

        if args.purge_mismatched:
            from .pricing import purge_mismatched_overseas

            stats = purge_mismatched_overseas(db.conn)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            # 删数据必须留痕：否则像 11-17 19:42 那样，4 条海外价消失后
            # 没有任何地方能查到是谁删的、删了什么（价格模块空掉的原因之一）
            record_price_run(db.conn, _iso_week(), {"purge_overseas": stats},
                             status="ok" if not stats["removed"] else "purged")
            return 0

        if args.import_excel:
            path = _Path(args.import_excel)
            if not path.exists():
                print(f"文件不存在：{path}")
                return 1
            stats = import_gw_excel(db.conn, path, observed_at=args.observed_at)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            # 一个价格点都没写进去不能报成功，否则用户以为导完了
            status = "error" if stats.get("error") else "ok"
            record_price_run(db.conn, _iso_week(), {"import_excel": stats}, status=status)
            return 3 if stats.get("error") else 0

        if args.import_csv:
            path = _Path(args.import_csv)
            if not path.exists():
                print(f"文件不存在：{path}")
                return 1
            stats = import_manual_csv(db.conn, path, cfg, observed_at=args.observed_at)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            record_price_run(db.conn, _iso_week(), {"import_csv": stats})
            # 一行都没导进去就是失败：渠道被拒、价格非法、商品对不上都算
            if stats["rows"] and stats["saved"] == 0:
                print("提示：没有任何一行导入成功，请检查 channel / price / sku_id 或 sku_name")
                return 3
            return 0

        rows = build_summary(db.conn, cfg, limit=int(args.limit),
                             line=args.line, status=args.status)
        print(render_summary(rows))
        total = db.conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        points = db.conn.execute("SELECT COUNT(*) c FROM price_points").fetchone()["c"]
        last_date = latest_price_date(db.conn)
        print(f"\n商品主表 {total} 个，价格点 {points} 条，最新观测 {last_date or '—'}")
        if last_date:
            import datetime as _dt

            age = (_dt.date.today() - _dt.date.fromisoformat(last_date)).days
            if age > 7:
                print(f"⚠ 价格数据已过期 {age} 天，建议跑 --import-excel 或 --fetch-overseas")
    finally:
        db.close()
    return 0


def cmd_history(args: argparse.Namespace, cfg) -> int:
    from .db import Database
    from .history import export_csv, fetch_history, render_text

    db = Database(cfg.db_path)
    try:
        rows = fetch_history(db.conn, days=int(args.days))
    finally:
        db.close()

    print(render_text(rows, int(args.days)))
    if args.csv:
        out = Path(args.csv)
        n = export_csv(rows, out)
        print(f"\n已导出 {n} 条到 {out}")
    return 0


def cmd_replay(args: argparse.Namespace, cfg) -> int:
    from .db import Database
    from .replay import parse_weights, run_replay

    base = {k: float(v) for k, v in (cfg.get("scoring.weights", {}) or {}).items()}
    try:
        new = parse_weights(args.weights, base)
    except ValueError as exc:
        print(f"参数错误：{exc}")
        return 2

    db = Database(cfg.db_path)
    try:
        print(run_replay(db.conn, base, new, top=int(args.top)))
        if args.weights:
            total_old = sum(base.values())
            total_new = sum(new.values())
            if abs(total_new - total_old) > 0.01:
                print(
                    f"\n注意：新权重合计 {total_new:g}，原合计 {total_old:g}；"
                    "总分不再是 0-100 量纲，阈值需按比例调整"
                )
    finally:
        db.close()
    return 0


def cmd_serve(args: argparse.Namespace, cfg) -> int:
    from .server import serve

    port = int(args.port or cfg.get("app.report_port", 8080))
    host = str(args.host or cfg.get("app.report_host", "0.0.0.0"))
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    try:
        serve(cfg.db_path, cfg.report_dir, port, host=host)
    except OSError as exc:
        print(f"启动失败：{exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infotwitter", description="战锤信息日报")
    parser.add_argument("--config", type=str, default=None, help="config.yaml 路径")
    parser.add_argument("--sources", type=str, default=None, help="sources.yaml 路径")
    parser.add_argument("--log-level", type=str, default="INFO")
    parser.add_argument(
        "--llm",
        type=str,
        default=None,
        choices=["deepseek", "ollama", "none", "mock"],
        help="临时覆盖 llm.provider（mock 用于离线验证链路）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="只跑采集")
    p_collect.set_defaults(func=cmd_collect)

    p_run = sub.add_parser("run", help="跑完整流水线")
    p_run.add_argument("--dry-run", action="store_true", help="只打印不发送")
    p_run.add_argument("--no-push", action="store_true", help="不推送")
    p_run.add_argument("--force", action="store_true", help="忽略当日已推送记录")
    p_run.add_argument("--reanalyze", action="store_true", help="清空历史抽取结果后重跑")
    p_run.add_argument("--no-collect", action="store_true", help="跳过采集，只用库内数据")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="查看台账与状态")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser("doctor", help="环境自检：配置/代理/密钥/数据库/定时任务")
    p_doctor.add_argument(
        "--probe-push", action="store_true",
        help="真发一条测试推送验证 token/通道有效（会在微信里收到一条消息）",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_sources = sub.add_parser("sources", help="按源查看产出统计")
    p_sources.add_argument("--days", type=int, default=7)
    p_sources.set_defaults(func=cmd_sources)

    p_login = sub.add_parser("login", help="登录需要账号态的站点（nga）")
    p_login.add_argument("site", type=str, help="站点标识，如 nga")
    p_login.add_argument("--wait", type=int, default=300, help="等待登录秒数")
    p_login.set_defaults(func=cmd_login)

    p_price = sub.add_parser("price", help="价格模块：导入调价表 / 查看价格摘要")
    p_price.add_argument("--import-excel", type=str, default=None, help="导入 GW 调价表 xlsx")
    p_price.add_argument("--import-csv", type=str, default=None,
                         help="导入人工快照 CSV（data/price_snapshots.csv）")
    p_price.add_argument("--purge-mismatched", action="store_true",
                         help="清理历史上因宽松匹配写入的错配海外价")
    p_price.add_argument("--observed-at", type=str, default=None,
                         help="观测时间覆写（YYYY-MM-DD；不写则用今天）")
    p_price.add_argument("--limit", type=int, default=20)
    p_price.add_argument("--line", type=str, default=None, help="40k|30k|aos|tow|other")
    p_price.add_argument("--fetch-overseas", action="store_true",
                          help="[已废弃] 逐商品搜索海外价；请改用 scripts/match_catalog.py 的目录流水线")
    # 0.55 会放过「Custodian Guard vs Custodian Wardens」这类兄弟商品（P-04）
    p_price.add_argument("--min-score", type=float, default=0.7,
                         help="名称匹配阈值（Jaccard，默认 0.7）")
    p_price.add_argument("--login", type=str, default=None, help="登录指定站点（xianyu|taobao）")
    p_price.add_argument("--wait", type=int, default=240, help="登录等待秒数")
    p_price.add_argument("--fetch-cn", type=str, default=None, help="抓国内价，逗号分隔（xianyu,taobao）")
    p_price.add_argument("--samples", type=int, default=8, help="每个商品取几条样本算中位价")
    p_price.add_argument("--show", action="store_true", help="显示浏览器窗口（调试用）")
    p_price.add_argument("--translate", action="store_true",
                          help="[已废弃] 逐条翻译品名；请改用 scripts/translate_products.py（术语表+批量）")
    p_price.add_argument("--status", type=str, default=None, help="preorder|new|active")
    p_price.set_defaults(func=cmd_price)

    p_history = sub.add_parser("history", help="历史推送汇总表")
    p_history.add_argument("--days", type=int, default=14)
    p_history.add_argument("--csv", type=str, default=None, help="导出 CSV 路径")
    p_history.set_defaults(func=cmd_history)

    p_replay = sub.add_parser("replay", help="用已有分项重算排名，零成本调权重")
    p_replay.add_argument("--weights", type=str, default=None,
                          help="覆盖权重，如 buzz=8,magnitude=32")
    p_replay.add_argument("--top", type=int, default=10)
    p_replay.set_defaults(func=cmd_replay)

    p_serve = sub.add_parser("serve", help="启动日报静态服务")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument(
        "--host", type=str, default=None,
        help="监听地址，默认 0.0.0.0（手机可访问）；只给本机用可写 127.0.0.1",
    )
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = load_config(
        Path(args.config) if args.config else None,
        Path(args.sources) if args.sources else None,
    )
    if args.llm:
        cfg.as_dict().setdefault("llm", {})["provider"] = args.llm
    setup_logging(cfg.log_dir, args.log_level)
    logger.debug("代理：%s", cfg.proxy)
    return int(args.func(args, cfg) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
