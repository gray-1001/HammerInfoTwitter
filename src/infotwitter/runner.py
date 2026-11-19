"""端到端编排：采集 → 分析 → 聚类 → 评分 → 选槽 → 排版 → 推送 → 台账。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from typing import Any

from .adapters import collect
from .config import ROOT, Config
from .content import backfill_short_docs, enrich_docs
from .db import CST, Database, now_iso
from .llm import build_llm
from .notify import ConsoleNotifier, build_notifier
from .pipeline import cluster_docs, merge_same_title, run_analysis, score_story, select_slots
from .pricing import build_slot5_rows
from .render import build_push_message, build_push_title, build_report
from .server import ensure_running

from pathlib import Path

log = logging.getLogger(__name__)


def _as_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


class RunLock:
    """跨进程互斥锁。

    计划任务的 MultipleInstances=IgnoreNew 只防同一任务重叠，防不住
    "手工重跑 + 计划任务同时跑"：两个进程都读到 pushed_today=False，
    就会各发一条（D-03）。这里用带 pid/时间戳的锁文件串行化。
    超过 stale_seconds 未释放视为遗留锁（进程被杀），自动接管。
    """

    def __init__(self, path: Path, stale_seconds: int = 2400):
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self.stale_seconds:
                    log.warning("清理遗留的运行锁（%.0f 秒未释放）", age)
                    self.path.unlink(missing_ok=True)
                    continue
                raise RuntimeError(f"运行锁被占用：{self.path}（{age:.0f} 秒）") from None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {now_iso()}")
            self.acquired = True
            return
        raise RuntimeError(f"无法获取运行锁：{self.path}")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            log.warning("释放运行锁失败：%s", exc)
        self.acquired = False


def run_daily(
    cfg: Config,
    dry_run: bool = False,
    skip_push: bool = False,
    force: bool = False,
    reanalyze: bool = False,
    skip_collect: bool = False,
) -> dict[str, Any]:
    lock = RunLock(cfg.db_path.parent / ".run.lock")
    try:
        lock.acquire()
    except RuntimeError as exc:
        log.error("已有另一个运行实例在进行，本次退出：%s", exc)
        # 锁冲突必须能被运维发现：退出码非 0 + 告警，否则任务计划程序永远显示成功（P1-6）
        _write_alert(cfg, datetime.now(CST).date().isoformat(),
                     f"运行锁被占用，本次未执行：{exc}")
        return {"status": "locked", "error": str(exc), "push": "locked"}

    db = Database(cfg.db_path)
    db.upsert_sources(cfg.sources)
    db.save_rubric(
        cfg.rubric_version,
        {
            "weights": cfg.get("scoring.weights", {}),
            "category_weights": cfg.get("scoring.category_weights", {}),
            "thresholds": cfg.get("thresholds", {}),
        },
    )
    if reanalyze:
        cleared = db.reset_analysis()
        log.info("已清空 %d 条历史抽取结果，将重新分析", cleared)

    run_id = db.start_run()
    today = datetime.now(CST).date().isoformat()
    stats: dict[str, Any] = {}
    llm = None

    try:
        # ---- 1. 采集 ----
        if skip_collect:
            log.info("=== 跳过采集（--no-collect）===")
            docs, inserted, failed = [], 0, []
        else:
            log.info("=== 采集 ===")
            docs, failed = collect(cfg, db)
            inserted = db.insert_raw_docs(docs)
            _record_source_stats(db, cfg, docs, failed)
            enabled = len([s for s in cfg.sources if s.get("active", True)])
            # 全部源都失败时不能拿着昨天的内容当今天发：那是最坏的静默失败（P1-6）
            if failed and not docs and len(failed) >= enabled:
                msg = f"全部 {enabled} 个源采集失败，本次不推送"
                log.error(msg)
                _write_alert(cfg, today, msg)
                stats["collect_dead"] = True
            log.info("采集 %d 条，新增 %d 条，失败源 %d 个", len(docs), inserted, len(failed))

        # ---- 2. LLM 分析（仅未分析过的）----
        # 窗口要覆盖最宽的源（中文源可到 168h），否则放宽采集也进不了流水线
        analysis_hours = max(cfg.max_collect_hours, 96)
        llm = build_llm(cfg)
        rows = db.recent_raw_docs(analysis_hours)
        # 标题型文档按源限额：超出的既不进分析也不成事件（C-22）。
        # 正文够长的（≥ analysis.min_body）不受限——那才是值得花 token 的内容。
        cap = int(cfg.get("analysis.title_only_per_source", 0) or 0)
        if cap:
            before = len(rows)
            rows = db.limit_title_only_docs(
                rows, per_source=cap, min_body=int(cfg.get("analysis.min_body", 400) or 400)
            )
            if before != len(rows):
                log.info("标题型文档限额：%d → %d 条（每源最多 %d 条）", before, len(rows), cap)
        pending = db.pending_analysis(rows)
        if pending:
            # 先用真实浏览器抓正文，再让 LLM 分析——否则它只能看着一句话标题瞎猜
            fetch_stats: dict[str, Any] = {}
            try:
                enrich_docs(cfg, db, [dict(r) for r in pending], stats=fetch_stats)
                # 列表页给不出（或曾经给错）日期的文档，用文章页 JSON-LD 补（P0-1）
                from .content import backfill_dates

                backfill_dates(cfg, db, limit=20, proxy=cfg.proxy)
            except Exception as exc:  # noqa: BLE001
                log.warning("正文抓取失败，降级用摘要分析：%s", exc)
                fetch_stats["error"] = str(exc)[:200]
            # 正文抓取是最大的静默失败来源：失败要分类计数，并让它在 alert 里可见
            if fetch_stats:
                stats["content_fetch"] = fetch_stats
                requested = int(fetch_stats.get("requested") or 0)
                failed = int(fetch_stats.get("failed") or 0)
                if requested >= 10 and failed / requested >= 0.5:
                    msg = (f"正文抓取失败率过高：{failed}/{requested} 失败"
                           f"（缓存命中 {fetch_stats.get('cached', 0)}）")
                    log.warning(msg)
                    _write_alert(cfg, today, msg)
            rows = db.recent_raw_docs(analysis_hours)
            if cap:
                rows = db.limit_title_only_docs(
                    rows, per_source=cap,
                    min_body=int(cfg.get("analysis.min_body", 400) or 400),
                )
            pending = db.pending_analysis(rows)

        if pending:
            # 粗略成本预估：实测约 800 tokens/篇，混合单价按 ¥3/百万 tokens 计
            est_tokens = len(pending) * 800
            log.info(
                "=== 分析 %d 条待处理文档（provider=%s，预估 %d tokens ≈ ¥%.2f）===",
                len(pending), llm.provider, est_tokens, est_tokens / 1_000_000 * 3,
            )
            payload = _as_dicts(pending)
            results = run_analysis(payload, llm, llm.enabled)
            for idx, res in results.items():
                db.save_analysis(payload[idx]["id"], res)
        else:
            log.info("无待分析文档，跳过 LLM 调用")

        # ---- 2b. 补抓：给已分析但正文过短的旧文档再抓一次 ----
        # 不补的话，在「跳转还原」「正文抓取」上线之前分析过的文档会永远停在
        # 一句话摘要（实测 173 条 Google News 就是这样）。
        try:
            bf = backfill_short_docs(cfg, db, limit=int(cfg.get("content.backfill_limit", 20)))
            if bf.get("candidates"):
                log.info(
                    "补抓：候选 %d 条，正文升级并重新排队 %d 条",
                    bf["candidates"], bf.get("requeued", 0),
                )
            stats["backfill"] = bf
        except Exception as exc:  # noqa: BLE001
            log.warning("补抓失败（不影响主流程）：%s", exc)

        # ---- 3. 聚类 ----
        rows = db.recent_raw_docs(cfg.max_collect_hours)
        docs_for_cluster = _as_dicts(rows)
        analyses: dict[int, dict[str, Any]] = {}
        for i, row in enumerate(rows):
            if row["analysis"]:
                try:
                    analyses[i] = json.loads(row["analysis"])
                except json.JSONDecodeError:
                    pass
        # 沿「文档归属链」复用已有 story_id：簇成员变化不再换 id（A-06）
        stories = cluster_docs(
            docs_for_cluster, analyses,
            doc_to_story=db.doc_story_map(),
            story_first_seen=db.story_first_seen(),
        )
        log.info("=== 聚类：%d 个事件 ===", len(stories))

        # ---- 4. 评分 ----
        interests = db.interest_map()
        for story in stories:
            story.update(score_story(story, cfg, interests))

        # 同一事件被聚成两条时，必须在写库之前合并（N-02）。
        # 放进 select_slots 是没用的：那里第一件事就是按 pushed_ids 过滤，
        # 被推过的那条已经消失，去重根本看不到它。
        stories, merged_ids = merge_same_title(
            stories, pushed_ids=db.pushed_story_ids(except_date=today if force else None)
        )
        if merged_ids:
            log.info(
                "合并同标题事件 %d 个（事件身份更久的一条保留）", len(merged_ids)
            )
        stats["merged"] = len(merged_ids)

        claims_written = 0
        for story in stories:
            db.insert_story(story)
            claims_written += db.replace_claims(story["id"], story.get("claims") or [])
            # 记下归属，下一轮据此认出同一个事件
            db.link_story_docs(story["id"], story.get("member_ids") or [])
        if claims_written:
            log.info("原子主张入库 %d 条（可通过 claim 数校验真实来源）", claims_written)

        # ---- 4b. 孤儿回收（A-03）----
        # 本轮没有产出的事件：文档已被别的事件收走（合并）就立即回收；
        # 只是滑出聚类窗口的，留到 backlog 回溯期结束再回收（N-06）。
        if not dry_run and stories:
            reclaimed = db.expire_absent_stories(
                {s["id"] for s in stories},
                keep_days=int(cfg.get("windows.backlog_days", 14)),
            )
            if reclaimed:
                log.info("回收孤儿事件 %d 个", reclaimed)
            stats["reclaimed"] = reclaimed
        else:
            stats["reclaimed"] = 0

        stories.sort(key=lambda s: s.get("importance_total") or 0, reverse=True)
        if stories:
            log.info(
                "Top5: %s",
                " | ".join(
                    f"{(s.get('title_zh') or s['canonical_title'])[:22]}({s['importance_total']})"
                    for s in stories[:5]
                ),
            )

        # ---- 5. 选槽 ----
        # 规则模式下 magnitude/actionability 维度天然偏低，按 0.55 缩放阈值，
        # 否则永远选不出内容（仅影响无 LLM 的降级运行）
        scale = 1.0 if llm.enabled else float(cfg.get("thresholds.rules_mode_scale", 0.55))
        pushed_ids = db.pushed_story_ids(except_date=today if force else None)
        current_ids = {s["id"] for s in stories}
        backlog_rows = db.backlog_candidates(
            cfg.backlog_days,
            float(cfg.get("thresholds.backlog_min_score", 45)) * scale,
            pushed_ids | current_ids,
        )
        slots = select_slots(stories, cfg, pushed_ids, _as_dicts(backlog_rows), threshold_scale=scale)

        # ---- 5b. 价格摘要（槽 5）----
        if cfg.get("pricing.auto_collect", True):
            try:
                from .pricing import auto_refresh

                price_stats = auto_refresh(db.conn, cfg)
                stats["price_auto"] = price_stats
            except Exception as exc:  # noqa: BLE001
                # 价格刷新失败不能影响日报主链路
                log.warning("价格自动刷新失败（不影响推送）：%s", exc)
        slots["product_rows"] = build_slot5_rows(db.conn, cfg, 5)
        # 价格覆盖情况：报告页要把"我们其实只有这么多数据"写清楚，而不是装作完整
        try:
            from .pricing import price_coverage

            slots["price_coverage"] = price_coverage(db.conn)
        except Exception as exc:  # noqa: BLE001
            log.debug("价格覆盖统计失败（不影响推送）：%s", exc)
            slots["price_coverage"] = {}
        total_products = db.conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
        total_points = db.conn.execute("SELECT COUNT(*) c FROM price_points").fetchone()["c"]
        log.info(
            "价格：商品主表 %d 个，价格点 %d 条，槽 5 展示 %d 行",
            total_products, total_points, len(slots["product_rows"]),
        )
        log.info(
            "=== 选槽：mode=%s 通用=%d 商品=%d 中文=%s ===",
            slots["mode"], len(slots["general"]), len(slots["product"]),
            "有" if slots["cn"] else "无",
        )

        # ---- 6. 排版 ----
        # 幂等判定必须发生在**渲染之前**：已推送过的日子再跑一次，如果先渲染，
        # 就会把 reports/{date}.html 与 latest.html 覆盖成重算后的另一份，
        # 读者点开推送里的链接看到的内容和当时发出去的已经不是一回事（D-02）。
        has_content = any(
            slots.get(k) for k in ("general", "product", "cn", "product_rows")
        )
        # 状态机收敛：上次若在「在途(2)」状态被杀（任务超时/断电），这一行永远不会变，
        # 而 pending_today 会让我们今天静默不推、退出码还是 0。启动时先把它判为失败，
        # 当天可以重试；同时在 alert 里留痕（P-01：状态机要有终态与恢复）。
        if not dry_run:
            stale = db.reconcile_inflight()
            if stale:
                msg = (f"发现 {len(stale)} 条上次未确认的在途投递"
                       f"（批次 {stale[0].get('sent_at')}），已标记为失败，本次将继续投递")
                log.warning(msg)
                _write_alert(cfg, today, msg)

        # 当天是否已经投递过（含"在途未确认"）。--force 表示明确要重发，不受此限。
        delivered_today = db.pushed_today(today) or db.pending_today(today)
        published_day = not dry_run and not force and delivered_today
        # --force 重推：站点根上已经是读者看过的版本，重推失败绝不能把它改掉（P1-5）
        reprint = not dry_run and force and delivered_today
        already = published_day and not skip_push
        if already:
            kind = "已推送" if db.pushed_today(today) else "有在途记录（上次可能未确认送达）"
            log.warning("今日%s（%s），跳过排版与推送。需重推请加 --force", kind, today)
            stats.update({
                "collected": len(docs), "inserted": inserted, "stories": len(stories),
            "skipped_collect": bool(skip_collect),
                "mode": slots["mode"], "general": len(slots["general"]),
                "product": len(slots["product"]), "cn": 1 if slots["cn"] else 0,
                "llm": llm.stats(), "push": "already_pushed",
            })
            db.finish_run(run_id, "ok", stats)
            return stats

        # 推送里的「完整版」链接依赖日报服务；没跑就临时拉起来，避免发出死链
        report_port = int(cfg.get("app.report_port", 8080))
        if not skip_push and not dry_run:
            if not ensure_running(cfg.db_path, cfg.report_dir, report_port):
                log.warning("日报服务未就绪，推送里的「完整版」链接可能打不开")
        # 日报服务以 report_dir 为站点根，所以 URL 不带 /reports/ 前缀（核查报告 BUG-01）
        report_url = f"{cfg.report_base_url()}/{today}.html"
        # 先确定通道，再按该通道的长度上限排版
        notifier = (
            ConsoleNotifier() if skip_push else build_notifier(cfg, force_preview=dry_run)
        )
        push_text = build_push_message(
            slots, today, report_url, notifier.content_type, notifier.max_bytes,
            allow_split=(notifier.name == "wecom_bot"),
        )
        push_title = build_push_title(slots, today)
        # dry-run 不得覆盖已发布的报告与预览：用户点开推送里的链接看到的必须是
        # 当时真实发送的那一份（核查报告 BUG-23）。
        # 已经推送过的日子即使加 --no-push 也只是"预览"，同样不能改写站点根（D-02）。
        out_dir = (cfg.report_dir / "_dryrun") if (dry_run or published_day or reprint) else cfg.report_dir
        if published_day:
            log.info("今日已投递，本次只生成预览到 %s，不覆盖站点根", out_dir.name)
        elif reprint:
            log.info("--force 重推：先生成到 %s，发送成功后再覆盖站点根", out_dir.name)
        # 把实际要发送的内容落盘，便于发送前肉眼核对排版
        preview_ext = "html" if notifier.content_type == "html" else "txt"
        preview_path = out_dir / f"push-{today}.{preview_ext}"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(push_text, encoding="utf-8")
        # 切换通道（txt↔html）后会留下上一份旧预览，看着像"这次发的"其实不是
        stale = out_dir / f"push-{today}.{'txt' if preview_ext == 'html' else 'html'}"
        if stale.exists():
            stale.unlink(missing_ok=True)
            log.info("清理上一次的旧预览 %s", stale.name)
        log.info("推送预览已写入 %s", preview_path)
        source_names = {
            r["id"]: r["name"] for r in db.conn.execute("SELECT id, name FROM sources")
        }
        effective_threshold = float(cfg.get("thresholds.qualified_score", 60)) * scale
        report_path = build_report(
            slots, today, out_dir,
            run_stats={"collected": len(docs), "inserted": inserted, "stories": len(stories),
                       "skipped_collect": bool(skip_collect)},
            llm_stats=llm.stats(),
            failed_sources=failed,
            source_names=source_names,
            effective_threshold=effective_threshold,
            llm_enabled=llm.enabled,
            write_latest=not (dry_run or published_day or reprint),
            weights=cfg.get("scoring.weights", {}) or {},
            cn_weights=cfg.get("scoring.cn_weights", {}) or {},
        )
        log.info("日报已生成：%s（报告 %d 字节 / 推送 %d 字节）",
                 report_path, report_path.stat().st_size, len(push_text.encode("utf-8")))

        stats.update({
            "collected": len(docs),
            "inserted": inserted,
            "stories": len(stories),
            "mode": slots["mode"],
            "general": len(slots["general"]),
            "product": len(slots["product"]),
            "cn": 1 if slots["cn"] else 0,
            "llm": llm.stats(),
            "report": str(report_path),
        })

        # ---- 7. 推送 + 台账 ----
        if skip_push:
            log.info("跳过推送（--no-push）")
            stats["push"] = "skipped"
        elif not dry_run and stats.get("collect_dead"):
            # 采集全挂：宁可不发，也不拿旧内容冒充今天（P1-6）。
            # 必须排在 has_content 之前：否则库里还有旧文档时会走「empty/正常」分支
            log.error("采集全部失败，跳过推送")
            stats["push"] = {"ok": False, "msg": "all sources failed", "channel": notifier.name}
        elif not has_content:
            log.warning("无任何可推内容，跳过推送")
            stats["push"] = "empty"
        elif dry_run:
            log.info("dry-run：不写入推送台账")
            stats["push"] = {"ok": True, "msg": "preview only", "channel": notifier.name}
        elif getattr(notifier, "preview", False):
            # push.dry_run=true：只打印，不能记账——否则"没发"会被记成"已送达"，当天真推被挡（P1-6）
            log.warning("push.dry_run=true：仅预览，不写台账、不算已推送")
            stats["push"] = {"ok": False, "msg": "push.dry_run 生效，未真正发送",
                             "channel": notifier.name}
        elif notifier.name == "console":
            # 没配真实通道时只打印。不能当成功：否则任务面板显示一切正常，
            # 而用户其实什么都没收到（D-09）
            log.warning("未配置真实推送通道（console），本次只打印不发送")
            stats["push"] = {"ok": False, "msg": "no channel configured", "channel": "console"}
        else:
            if not ensure_running(cfg.db_path, cfg.report_dir, report_port):
                log.error("日报服务未就绪，发出去的链接会打不开，本次不推送")
                stats["push"] = {"ok": False, "msg": "report server down", "channel": notifier.name}
                _write_alert(cfg, today, "日报服务未就绪，已跳过推送")
            else:
                # 先在台账里占位（状态 2 = 在途），再发送：进程若在"已发送、未记账"
                # 之间被杀，下次运行能看出"当天有过一次未确认的投递"而不是重发一遍（D-03）
                rows, story_ids = _push_rows(db, today, slots, push_text, notifier.name)
                sent_at = now_iso()
                db.begin_push_batch(rows, sent_at)
                ok, msg = _send_with_retry(notifier, push_title, push_text, cfg)
                db.finish_push_batch(sent_at, ok)
                # 重推失败时不能把已经推送过的事件降回 scored：那会污染去重依据（P1-5）
                if not reprint:
                    db.set_stories_status(story_ids, "pushed" if ok else "scored")
                elif ok:
                    db.set_stories_status(story_ids, "pushed")
                log.info("推送结果：%s / %s", ok, msg)
                stats["push"] = {"ok": ok, "msg": msg, "channel": notifier.name}
                # 重推成功才把新版本发布到站点根；失败时读者看到的仍是当时那一份（P1-5）
                if reprint:
                    if ok:
                        published = _publish_report(cfg, report_path, today)
                        log.info("重推成功，站点报告已更新：%s", published)
                        stats["republished"] = str(published)
                    else:
                        log.warning("重推失败，站点报告保持原样（未覆盖）")
                if not ok:
                    log.error("推送失败：%s", msg)
                    _write_alert(cfg, today, msg)

        expired = pruned = 0
        if dry_run:
            # 删数据是生产副作用，dry-run 不应触发
            log.info("dry-run：跳过过期清理与 VACUUM")
        else:
            expired = db.expire_old_stories(cfg.backlog_days)
            if expired:
                log.info("清理过期事件 %d 个", expired)
            pruned = db.prune_raw_docs(int(cfg.get("retention.raw_docs_days", 14)))
            if pruned:
                log.info("清理过期原始文档 %d 条", pruned)
            cache_pruned = db.prune_content_cache(int(cfg.get("retention.content_cache_days", 14)))
            if cache_pruned:
                log.info("清理过期正文缓存 %d 条", cache_pruned)
            # 孤儿 story：被拆分/重组后换下的旧 id，已无文档归属，留着会污染统计。
            # keep_days=0 是安全的：本函数在 insert_story + link_story_docs 之后才执行，
            # 本轮建的事件都已经绑好文档，不会误删。
            orphans = db.prune_orphan_stories(0)
            if orphans:
                log.info("清理孤儿事件 %d 个", orphans)
            if pruned or cache_pruned:
                db.vacuum()
        stats["expired"] = expired
        stats["pruned"] = pruned
        db.finish_run(run_id, "ok", stats)
        return stats

    except Exception as exc:  # noqa: BLE001
        log.exception("运行失败")
        # 生成阶段的失败（采集全挂、LLM 欠费、模板异常）同样要落告警，
        # 不能只有推送失败才写 alert.log（D-10）
        _write_alert(cfg, today, f"运行失败：{type(exc).__name__}: {exc}")
        db.finish_run(run_id, "error", stats, str(exc))
        raise
    finally:
        if llm is not None:
            try:
                llm.close()
            except Exception:  # noqa: BLE001
                pass
        db.close()
        lock.release()


def _send_with_retry(
    notifier: Any, title: str, text: str, cfg: Config, attempts: int = 3
) -> tuple[bool, str]:
    """推送失败重试（设计 §12「内部重试 3 次」）。

    PushPlus 免费档限 5 条/分钟，所以退避取 4s / 9s，避免重试本身触发限频。
    """
    delays = (4.0, 9.0)
    last_msg = ""
    for i in range(max(1, attempts)):
        try:
            ok, msg = notifier.send(title, text)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        if ok:
            return True, msg
        last_msg = msg
        if i < len(delays):
            log.warning("推送失败（%s），%.0fs 后重试", msg, delays[i])
            time.sleep(delays[i])
    return False, last_msg


def _record_source_stats(db: Any, cfg: Any, docs: list[dict[str, Any]], failed: list[str]) -> None:
    """按源记产出，供 `run.py sources` 与排查使用。"""
    from collections import Counter

    from .db import CST as _CST
    from datetime import datetime as _dt

    per: dict[str, tuple[int, bool, int]] = {}
    body_len: dict[str, list[int]] = {}
    for d in docs:
        sid = str(d.get("source_id") or "")
        if not sid:
            continue
        body_len.setdefault(sid, []).append(len(d.get("body") or ""))
    for sid, lens in body_len.items():
        per[sid] = (len(lens), True, sum(lens) // max(1, len(lens)))
    for f in failed:
        sid = str(f).split()[0] if f else ""
        if sid and sid not in per:
            per[sid] = (0, False, 0)
        elif sid:
            docs_n, _ok, avg = per[sid]
            per[sid] = (docs_n, False, avg)
    try:
        db.record_source_stats(_dt.now(_CST).date().isoformat(), per)
    except Exception as exc:  # noqa: BLE001
        log.warning("记录源产出统计失败：%s", exc)


def _write_alert(cfg: Config, today: str, reason: str) -> None:
    """推送失败留痕：Windows 任务面板只看退出码，告警必须落到文件。"""
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        path = cfg.log_dir / "alert.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()}\t{today}\t今日日报推送失败：{reason}\n")
    except OSError as exc:  # noqa: BLE001
        log.warning("写告警日志失败：%s", exc)


def _publish_report(cfg: Config, report_path: Path, today: str) -> Path:
    """把暂存目录里的日报发布到站点根，并顺带更新 latest.html。

    只在 --force 重推成功之后调用：先渲染后发送、失败就不覆盖，
    读者点开推送里的链接看到的永远是真正发出过的那一份（P1-5）。
    """
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.report_dir / f"{today}.html"
    shutil.copyfile(report_path, target)
    latest = cfg.report_dir / "latest.html"
    shutil.copyfile(report_path, latest)
    return target


def _push_rows(
    db: Database, today: str, slots: dict[str, Any], payload: str, channel: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """构造这一批台账行，并返回需要标记为已推送的 story id。

    台账记的是「这次到底发了什么」，不是「选中了什么」。发送条件里包含
    `product_rows`（只有价格表也能发），写台账时也必须认它，否则纯价格日
    发了却不记账，`pushed_today` 为 False，同一天第二次运行会再推一遍（D-01）。
    """
    rows: list[dict[str, Any]] = []
    story_ids: list[str] = []
    for idx, item in enumerate(slots.get("general") or [], start=1):
        rows.append(
            {
                "push_date": today, "slot": idx, "story_id": item["id"],
                "title": item.get("title_zh") or item.get("canonical_title"),
                # 记「读者看到的那个分」：二级补位是衰减分。记原始分会让
                # history 与推送/报告页对不上（N-03）
                "importance": item.get("_decayed") or item.get("importance_total"),
                "channel": channel, "payload": payload,
            }
        )
        story_ids.append(item["id"])

    if slots.get("product") or slots.get("product_rows"):
        price_ids = [r.get("id") for r in (slots.get("product_rows") or []) if r.get("id")]
        prod_story_ids = [p["id"] for p in (slots.get("product") or [])]
        rows.append(
            {
                "push_date": today, "slot": 5, "story_id": None,
                # 标题必须和正文一致：正文是「模型价格速览」，台账不能记成「本周模型快讯」
                "title": "模型价格速览",
                "sku_ids": price_ids or prod_story_ids,
                "channel": channel, "payload": payload,
            }
        )
        # 商品槽的 story 不写进 story_ids：正文只展示价格表，读者没有看到它的新闻卡。
        # 以前这里 extend 了，导致 11-19 那天 ba5b6675（Chaos Knights 58.4）被标「已推送」
        # 却从没出现在正文里——内容被静默消费，以后也不会再推（P-15）。
        # 它们的 id 已经记在 sku_ids 里，需要追溯时查得到。

    if slots.get("cn"):
        cn = slots["cn"]
        rows.append(
            {
                "push_date": today, "slot": 6, "story_id": cn["id"],
                "title": cn.get("title_zh") or cn.get("canonical_title"),
                # 槽 6 读者看到的是中文社区卡的分，不是通识分（P2-5）
                "importance": cn.get("cn_score") if cn.get("cn_score") is not None
                else cn.get("importance_total"),
                "channel": channel, "payload": payload,
            }
        )
        story_ids.append(slots["cn"]["id"])

    if not rows:
        # 兜底：真的什么都没发出去时也要留一行，否则"已推送"这件事对系统不可见
        rows.append(
            {
                "push_date": today, "slot": 0, "story_id": None,
                "title": "（本次无内容，仅记录投递）",
                "channel": channel, "payload": payload,
            }
        )
    return rows, story_ids


def _record_pushes(
    db: Database, today: str, slots: dict[str, Any], payload: str, channel: str, ok: bool
) -> None:
    """一次性写完整批台账（保留给测试与维护脚本用）。

    日常运行走 `begin_push_batch` + `finish_push_batch`：先占位再发送，
    避免"送达了但没记账"或"记了一半"（D-03/D-11）。
    """
    rows, story_ids = _push_rows(db, today, slots, payload, channel)
    sent_at = now_iso()
    db.begin_push_batch(rows, sent_at)
    db.finish_push_batch(sent_at, ok)
    db.set_stories_status(story_ids, "pushed" if ok else "scored")
