"""SQLite 数据访问层。"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    """统一使用北京时间 ISO 字符串，便于人读与排序。"""
    return datetime.now(CST).replace(microsecond=0).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


class Database:
    def __init__(self, path: Path, migrate: bool = True, read_only: bool = False):
        """migrate=False：只拿连接、不做建表/迁移。

        反馈接口每次点击都会连一次库，如果每次都跑 `executescript(schema)` + 迁移，
        就等于每次点击都抢一次写锁；日更任务正在写或 VACUUM 时会直接 500（D-05）。

        read_only=True：以只读模式打开（`mode=ro`），用于审计脚本与只读回归测试。
        只读连接既不会建表也不会写库，误操作会直接报错而不是悄悄改数据。
        """
        self.path = Path(path)
        if read_only:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout = 3000")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # 并发写时最多等 3 秒再报 database is locked，而不是立刻失败
        self.conn.execute("PRAGMA busy_timeout = 3000")
        if migrate:
            self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """对已存在的库补齐新增列（CREATE TABLE IF NOT EXISTS 不会改旧表）。

        全部幂等：先查 PRAGMA table_info，缺哪列补哪列。
        """
        additions: dict[str, dict[str, str]] = {
            "raw_docs": {
                "discovered_at": "TEXT",
                "derived_tier": "TEXT",
                "publisher_domain": "TEXT",
                "time_source": "TEXT DEFAULT 'feed'",
            },
            "sources": {"fake_reports": "INTEGER NOT NULL DEFAULT 0"},
            "products": {
                # 中文名的来源与时间：翻译要可追溯，换了模型/术语表能看出是哪一版产的
                "name_zh_source": "TEXT",
                "name_zh_at": "TEXT",
            },
            "overseas_catalog": {
                # 目录商品页上的 GW 官方货号：按码匹配比按名字匹配可靠得多，
                # 但要先进详情页才能拿到，所以单独一列、可空
                "product_code": "TEXT",
                # 首次出现在目录里的时间：每周新品就是「本周 first_seen 的 URL」
                "first_seen_at": "TEXT",
            },
            "stories": {
                "published_at": "TEXT",
                "confidence": "TEXT",
                "claim_conflict": "INTEGER NOT NULL DEFAULT 0",
                # 时间来源要落库：backlog 查询靠它排除「无发布时间的常青页面」（P3-2）
                "time_source": "TEXT",
            },
        }
        for table, cols in additions.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for name, decl in cols.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    log.info("迁移：%s 新增列 %s", table, name)
        # 老数据回填：discovered_at 缺省取抓取时间
        self.conn.execute(
            "UPDATE raw_docs SET discovered_at = COALESCE(discovered_at, fetched_at) "
            "WHERE discovered_at IS NULL"
        )
        # 目录首次出现时间：老行回填成 crawled_at（历史数据里两者等价）
        self.conn.execute(
            "UPDATE overseas_catalog SET first_seen_at = COALESCE(first_seen_at, crawled_at)"
            " WHERE first_seen_at IS NULL"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_discovered ON raw_docs(discovered_at)"
        )
        # 数据修复（C-01）：浏览器源的 published_at 曾被写成抓取时刻，
        # 这类"时间未知"的行必须改回 NULL + time_source='fetch'，否则会以满分时效进日报。
        # 幂等：改完之后 time_source='fetch'，不会再命中。
        cur = self.conn.execute(
            "UPDATE raw_docs SET published_at = NULL, time_source = 'fetch' "
            "WHERE published_at = fetched_at AND COALESCE(time_source, '') <> 'fetch' "
            "AND source_id IN (SELECT id FROM sources WHERE fetch_method = 'browser')"
        )
        if cur.rowcount:
            log.warning(
                "数据修复：%d 条浏览器源文档的发布时间实为抓取时刻，已标记为「时间未知」",
                cur.rowcount,
            )
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- sources ----------
    def upsert_sources(self, sources: Iterable[dict[str, Any]]) -> None:
        with self.tx() as conn:
            for s in sources:
                conn.execute(
                    """
                    INSERT INTO sources (id, name, url, domain, category, tier, lang,
                                         fetch_method, weight, active, note)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name, url=excluded.url, category=excluded.category,
                        tier=excluded.tier, lang=excluded.lang,
                        fetch_method=excluded.fetch_method, weight=excluded.weight,
                        active=excluded.active, note=excluded.note
                    """,
                    (
                        s["id"], s["name"], s.get("url") or f"googlenews:{s.get('query', '')}",
                        s.get("domain", "warhammer"),
                        s["category"], s["tier"], s["lang"], s["type"],
                        float(s.get("weight", 1.0)), 1 if s.get("active", True) else 0,
                        s.get("note"),
                    ),
                )

    def mark_source_result(self, source_id: str, ok: bool) -> None:
        with self.tx() as conn:
            if ok:
                conn.execute(
                    "UPDATE sources SET last_ok_at=?, fail_count=0 WHERE id=?",
                    (now_iso(), source_id),
                )
            else:
                conn.execute(
                    "UPDATE sources SET fail_count=fail_count+1 WHERE id=?", (source_id,)
                )

    # ---------- raw_docs ----------
    def insert_raw_docs(self, docs: list[dict[str, Any]]) -> int:
        """按 (source_id, content_hash) 去重插入，返回新增条数。

        已存在的行不会重新插入，但**允许补全发布时间**：适配器后来才学会解析日期时，
        老行如果一直是 NULL/抓取时刻，就永远补不回来（核查报告 C-01 的连带问题）。
        """
        inserted = 0
        ts = now_iso()
        with self.tx() as conn:
            for d in docs:
                cur = conn.execute(
                    """
                    INSERT INTO raw_docs
                        (source_id, url, title, body, lang, published_at, fetched_at,
                         discovered_at, content_hash, derived_tier, publisher_domain,
                         time_source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id, content_hash) DO UPDATE SET
                        published_at = COALESCE(raw_docs.published_at, excluded.published_at),
                        time_source = CASE
                            WHEN raw_docs.published_at IS NULL
                                 AND excluded.published_at IS NOT NULL
                            THEN excluded.time_source ELSE raw_docs.time_source END,
                        derived_tier = COALESCE(raw_docs.derived_tier, excluded.derived_tier),
                        publisher_domain = COALESCE(raw_docs.publisher_domain,
                                                    excluded.publisher_domain),
                        -- 适配器后来学会抓正文时（如 Reddit 补自述），老行也要能升级；
                        -- 留 100 字余量，避免每次抓取都因抖动重写（C-17）
                        body = CASE
                            WHEN length(coalesce(excluded.body, ''))
                                 > length(coalesce(raw_docs.body, '')) + 100
                            THEN excluded.body ELSE raw_docs.body END,
                        -- 正文升级后要让下一轮重新分析，否则摘要还是按标题编的
                        analyzed_at = CASE
                            WHEN length(coalesce(excluded.body, '')) >= 400
                                 AND length(coalesce(excluded.body, ''))
                                     > length(coalesce(raw_docs.body, '')) + 100
                            THEN NULL ELSE raw_docs.analyzed_at END
                    """,
                    (
                        d["source_id"], d["url"], d.get("title"), d.get("body"),
                        d.get("lang"), d.get("published_at"), d.get("fetched_at") or ts,
                        d.get("discovered_at") or d.get("fetched_at") or ts,
                        d["content_hash"], d.get("derived_tier"),
                        d.get("publisher_domain"), d.get("time_source") or "feed",
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def record_source_stats(
        self, stat_date: str, per_source: dict[str, tuple[int, bool, int]]
    ) -> int:
        """记录每个源本轮的产出：(docs, ok, avg_body)。

        以前只有 fail_count，回答不了「这个源这周到底产出多少、正文多长」。
        """
        rows = [
            (stat_date, sid, docs, 1 if ok else 0, avg_body, now_iso())
            for sid, (docs, ok, avg_body) in per_source.items()
        ]
        if not rows:
            return 0
        with self.tx() as conn:
            conn.executemany(
                "INSERT INTO source_stats (stat_date, source_id, docs, ok, avg_body, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(stat_date, source_id) DO UPDATE SET "
                "docs=excluded.docs, ok=excluded.ok, avg_body=excluded.avg_body, "
                "updated_at=excluded.updated_at",
                rows,
            )
        return len(rows)

    def source_stats_summary(self, days: int = 7) -> list[sqlite3.Row]:
        """最近 N 天按源汇总：总条数、失败次数、平均正文、活跃天数。"""
        since = (datetime.now(CST) - timedelta(days=days)).replace(microsecond=0).date().isoformat()
        return list(
            self.conn.execute(
                """
                SELECT source_id,
                       SUM(docs) AS docs,
                       SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS fails,
                       CAST(AVG(CASE WHEN ok=1 THEN avg_body END) AS INTEGER) AS avg_body,
                       COUNT(*) AS days
                FROM source_stats WHERE stat_date >= ?
                GROUP BY source_id ORDER BY docs DESC
                """,
                (since,),
            )
        )

    def recent_raw_docs(self, hours: int) -> list[sqlite3.Row]:
        since = (datetime.now(CST) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        # tier 取「真实发布域名定级」，并对被多次举报为假的源降一级（设计 §11 / §6.3）
        effective_tier = (
            "CASE WHEN s.fake_reports >= 3 THEN "
            "  CASE COALESCE(r.derived_tier, s.tier)"
            "    WHEN 'L0' THEN 'L1' WHEN 'L1' THEN 'L2' WHEN 'L2' THEN 'L3'"
            "    ELSE COALESCE(r.derived_tier, s.tier) END "
            "  ELSE COALESCE(r.derived_tier, s.tier) END"
        )
        return list(
            self.conn.execute(
                f"""
                SELECT r.*, s.name AS source_name, {effective_tier} AS tier,
                       s.category, s.lang AS source_lang, s.weight AS source_weight
                FROM raw_docs r JOIN sources s ON s.id = r.source_id
                -- 窗口按「我们什么时候拿到的」算，而不是文章本身的日期：
                -- 迟到的新闻（文章旧、今天才抓到）也要能进分析，否则白抓（P1-3）
                WHERE COALESCE(r.discovered_at, r.published_at, r.fetched_at) >= ?
                ORDER BY COALESCE(r.discovered_at, r.published_at, r.fetched_at) DESC
                """,
                (since,),
            )
        )

    def pending_analysis(self, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        return [r for r in rows if not r["analyzed_at"]]

    def limit_title_only_docs(
        self, rows: list[sqlite3.Row], per_source: int = 0, min_body: int = 400
    ) -> list[sqlite3.Row]:
        """标题型文档按源限额，超出的不送分析、也不参与聚类（C-22）。

        背景：Reddit + B 站每天约 266 条，占了 76% 的分析量，却几乎不产出
        可用条目（≥55 分的 13 条里只有 1 条来自它们）。正文够长的文档不受限，
        因为它们才有真内容可分析。

        rows 已按发现时间倒序，所以每个源保留的是最新那几条。
        """
        if per_source <= 0:
            return rows
        kept: list[sqlite3.Row] = []
        counts: dict[str, int] = {}
        for r in rows:
            if len(r["body"] or "") >= min_body:
                kept.append(r)
                continue
            sid = r["source_id"]
            if counts.get(sid, 0) >= per_source:
                continue
            counts[sid] = counts.get(sid, 0) + 1
            kept.append(r)
        return kept

    def save_analysis(self, raw_id: int, analysis: dict[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE raw_docs SET analysis=?, analyzed_at=? WHERE id=?",
                (json.dumps(analysis, ensure_ascii=False), now_iso(), raw_id),
            )

    def reset_analysis(self) -> int:
        """清空抽取结果（切换模型或重跑评分时使用）。"""
        with self.tx() as conn:
            cur = conn.execute("UPDATE raw_docs SET analysis=NULL, analyzed_at=NULL")
        return cur.rowcount

    # ---------- stories ----------
    def insert_story(self, story: dict[str, Any]) -> None:
        """写入/更新事件。

        用 upsert 而不是 INSERT OR REPLACE：REPLACE 会先删后插，
        把上一轮写好的 status（pushed/skipped）静默重置回 scored（核查报告 BUG-07）。
        """
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO stories
                    (id, canonical_title, title_zh, event_type, entities, category,
                     impact_scope, first_seen_at, published_at, last_seen_at, source_ids,
                     authenticity, authenticity_note, confidence, claim_conflict,
                     importance_total, score_breakdown,
                     rubric_version, summary, why_matters, quote_en, quote_zh,
                     primary_url, status, time_source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    canonical_title=excluded.canonical_title,
                    title_zh=excluded.title_zh,
                    event_type=excluded.event_type,
                    entities=excluded.entities,
                    category=excluded.category,
                    impact_scope=excluded.impact_scope,
                    last_seen_at=excluded.last_seen_at,
                    source_ids=excluded.source_ids,
                    authenticity=excluded.authenticity,
                    authenticity_note=excluded.authenticity_note,
                    confidence=excluded.confidence,
                    claim_conflict=excluded.claim_conflict,
                    importance_total=excluded.importance_total,
                    score_breakdown=excluded.score_breakdown,
                    rubric_version=excluded.rubric_version,
                    summary=excluded.summary,
                    why_matters=excluded.why_matters,
                    quote_en=excluded.quote_en,
                    quote_zh=excluded.quote_zh,
                    published_at=excluded.published_at,
                    primary_url=excluded.primary_url,
                    time_source=excluded.time_source,
                    status = CASE
                        -- 已推送的保持 pushed（去重依据）
                        WHEN stories.status = 'pushed' THEN 'pushed'
                        -- 被回收/跳过的，本轮又产出了就复活，否则会永远推不出去
                        ELSE excluded.status END
                    -- 故意不更新 first_seen_at（首次出现时间一经确定不再漂移）
                """,
                (
                    story["id"], story["canonical_title"], story.get("title_zh"),
                    story.get("event_type", "other"),
                    json.dumps(story.get("entities") or [], ensure_ascii=False),
                    story.get("category", "other"), story.get("impact_scope", "global"),
                    story["first_seen_at"], story.get("published_at"),
                    story.get("last_seen_at", story["first_seen_at"]),
                    json.dumps(story.get("source_ids") or [], ensure_ascii=False),
                    story.get("authenticity", "L2"), story.get("authenticity_note"),
                    story.get("confidence"), int(story.get("claim_conflict") or 0),
                    story.get("importance_total"), story.get("score_breakdown"),
                    story.get("rubric_version"), story.get("summary"),
                    story.get("why_matters"), story.get("quote_en"), story.get("quote_zh"),
                    story.get("primary_url"), story.get("status", "new"),
                    story.get("time_source") or "feed",
                ),
            )

    # ---------- story_docs：事件与文档的归属链 ----------
    def doc_story_map(self) -> dict[int, str]:
        """raw_doc_id → story_id。用于下一次聚类时复用已有事件 id（A-06）。"""
        rows = self.conn.execute("SELECT raw_doc_id, story_id FROM story_docs")
        return {int(r["raw_doc_id"]): r["story_id"] for r in rows}

    def story_first_seen(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT id, first_seen_at FROM stories")
        return {r["id"]: r["first_seen_at"] for r in rows}

    def link_story_docs(self, story_id: str, raw_doc_ids: list[int]) -> int:
        """把本轮该事件的所有文档挂到 story_id 上（覆盖旧归属）。"""
        ids = [int(i) for i in raw_doc_ids if i is not None]
        if not ids:
            return 0
        ts = now_iso()
        with self.tx() as conn:
            for rid in ids:
                conn.execute(
                    "INSERT INTO story_docs (raw_doc_id, story_id, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(raw_doc_id) DO UPDATE SET story_id=excluded.story_id,"
                    " updated_at=excluded.updated_at",
                    (rid, story_id, ts),
                )
        return len(ids)

    def expire_absent_stories(self, seen_ids: set[str], keep_days: int = 14) -> int:
        """回收本轮没有产出、且不该继续留在候选里的 story（A-03）。

        两条判定，满足其一即回收：
        1. 它已经没有任何挂靠文档（每篇都被别的事件收走 = 被合并了）——立即回收；
        2. 最近一次出现早于 backlog 回溯期（`keep_days`）——已经滑出所有候选窗口。

        第 2 条用的是 backlog 窗口而不是固定 24 小时：滑出聚类窗口、但仍在
        backlog 回溯期内的 story 是**合法的历史候选**，不该被提前回收，
        否则 backlog 这一级降级永远轮不到（N-06）。

        - 空运行（seen_ids 为空）绝不清理：那通常意味着采集/聚类整体失败。
        - `pushed` 不会被清理（已经送达，回收没有意义）。
        """
        if not seen_ids:
            return 0
        cutoff = (datetime.now(CST) - timedelta(days=max(1, keep_days))).replace(
            microsecond=0
        ).isoformat()
        ids = list(seen_ids)
        placeholders = ",".join("?" * len(ids))
        with self.tx() as conn:
            cur = conn.execute(
                f"UPDATE stories SET status='expired' "
                f"WHERE status IN ('scored','skipped','new') "
                f"AND id NOT IN ({placeholders}) "
                f"AND ( last_seen_at < ? "
                f"      OR NOT EXISTS (SELECT 1 FROM story_docs d WHERE d.story_id = stories.id) )",
                (*ids, cutoff),
            )
        return cur.rowcount

    # ---------- claims ----------
    def replace_claims(self, story_id: str, claims: list[dict[str, Any]]) -> int:
        """用本轮抽取结果覆盖该 story 的原子主张（幂等）。"""
        with self.tx() as conn:
            conn.execute("DELETE FROM claims WHERE story_id=?", (story_id,))
            n = 0
            for c in claims or []:
                text = str(c.get("text") or "").strip()
                if not text:
                    continue
                raw_id = c.get("raw_doc_id")
                if raw_id is not None:
                    # 原始文档可能已被保留策略清理，悬空外键会导致整批写入失败
                    exists = conn.execute(
                        "SELECT 1 FROM raw_docs WHERE id=?", (raw_id,)
                    ).fetchone()
                    if not exists:
                        raw_id = None
                conn.execute(
                    "INSERT INTO claims (story_id, claim_text, claim_type, raw_doc_id,"
                    " evidence_span, support_count, contradict) VALUES (?,?,?,?,?,?,?)",
                    (
                        story_id, text, str(c.get("type") or "statement"),
                        raw_id, str(c.get("evidence") or ""),
                        int(c.get("support_count", 1)), int(c.get("contradict", 0)),
                    ),
                )
                n += 1
            return n

    def claim_count(self) -> int:
        """测试与维护脚本用：统计某个 story 的证据条数。"""
        row = self.conn.execute("SELECT COUNT(*) c FROM claims").fetchone()
        return int(row["c"] or 0)

    def update_story_fields(self, story_id: str, **fields: Any) -> None:
        """测试与维护脚本用：按字段名白名单更新 story。"""
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as conn:
            conn.execute(f"UPDATE stories SET {cols} WHERE id=?", (*fields.values(), story_id))

    def pushed_story_ids(self, since_days: int = 30, except_date: str | None = None) -> set[str]:
        """返回近期**成功送达**的 story id。

        只统计 delivery_ok=1：推送失败的内容必须能被下次重试捞回来，
        否则一旦 webhook 抖一下，当天最好的几条就永久丢失了。
        """
        since = (datetime.now(CST) - timedelta(days=since_days)).date().isoformat()
        rows = self.conn.execute(
            """
            SELECT story_id FROM pushes
            WHERE push_date >= ? AND story_id IS NOT NULL AND delivery_ok = 1
              AND (? IS NULL OR push_date != ?)
            """,
            (since, except_date, except_date),
        )
        return {r["story_id"] for r in rows}

    def backlog_candidates(self, days: int, min_score: float, exclude: set[str]) -> list[sqlite3.Row]:
        since = (datetime.now(CST) - timedelta(days=days)).replace(microsecond=0).isoformat()
        rows = self.conn.execute(
            """
            SELECT * FROM stories
            WHERE status IN ('scored','skipped')
              AND first_seen_at >= ?
              AND COALESCE(importance_total, 0) >= ?
              -- 必须有文档归属：孤儿 story 是历史遗留的重复记录，
              -- 内容早已被别的 story 接管，推出去就是重复推送
              AND id IN (SELECT DISTINCT story_id FROM story_docs
                         WHERE story_id IS NOT NULL AND story_id != '')
              -- 时间未知的事件不能从「历史补位」绕回推送：一级筛选里已经排除了它们，
              -- 这里不排除就等于开了一扇后门（P3-2）
              AND NOT (COALESCE(time_source, '') = 'fetch' AND published_at IS NULL)
            ORDER BY importance_total DESC
            """,
            (since, min_score),
        )
        return [r for r in rows if r["id"] not in exclude]

    def prune_orphan_stories(self, keep_days: int = 1) -> int:
        """删除没有任何文档归属的 story。

        产生原因见 pipeline.assign_stable_ids：拆分场景会另起 id，旧记录不再
        被任何文档指向。这些记录仍会出现在报告页和 history 里，且 status 仍是
        'scored'，不清理就会污染统计。

        三条安全线：
        - 只删 first_seen 早于 keep_days 的。调用方必须在 link_story_docs 之后执行，
          否则会把本轮刚建、文档还没绑上的事件误删；
        - 绝不动已经推送过的（pushes 有记录），保留台账可追溯；
        - 先删 claims 再删 story：claims.story_id 有外键，顺序反了会直接报错。
        """
        cutoff = (datetime.now(CST) - timedelta(days=keep_days)).replace(microsecond=0).isoformat()
        orphan_cond = """
            first_seen_at < ?
            AND id NOT IN (SELECT DISTINCT story_id FROM story_docs
                           WHERE story_id IS NOT NULL AND story_id != '')
            AND id NOT IN (SELECT story_id FROM pushes WHERE story_id IS NOT NULL)
        """
        with self.tx() as conn:
            conn.execute(f"DELETE FROM claims WHERE story_id IN (SELECT id FROM stories WHERE {orphan_cond})",
                         (cutoff,))
            cur = conn.execute(f"DELETE FROM stories WHERE {orphan_cond}", (cutoff,))
            removed = cur.rowcount
        return removed

    def expire_old_stories(self, days: int) -> int:
        cutoff = (datetime.now(CST) - timedelta(days=days)).replace(microsecond=0).isoformat()
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE stories SET status='expired' "
                "WHERE status IN ('scored','skipped','new') AND first_seen_at < ?",
                (cutoff,),
            )
        return cur.rowcount

    def prune_raw_docs(self, keep_days: int) -> int:
        """清理过老的原始文档，避免库无限膨胀（stories 已保留结论）。

        必须先解绑 `claims.raw_doc_id`：它是外键且 foreign_keys=ON，
        直接删会让整个日更任务在清理这一步抛 IntegrityError 并标记失败，
        连带后面的缓存清理与 VACUUM 都不再执行（核查报告 S-01）。
        """
        cutoff = (datetime.now(CST) - timedelta(days=keep_days)).replace(microsecond=0).isoformat()
        with self.tx() as conn:
            # 保留期也按「入库时间」算：文章旧但今天才抓到的，不能被立刻删掉（P1-3）
            conn.execute(
                "UPDATE claims SET raw_doc_id = NULL WHERE raw_doc_id IN "
                "(SELECT id FROM raw_docs WHERE COALESCE(discovered_at, fetched_at) < ?)",
                (cutoff,),
            )
            cur = conn.execute(
                "DELETE FROM raw_docs WHERE COALESCE(discovered_at, fetched_at) < ?", (cutoff,)
            )
            # story_docs 没有外键，但留着指向已删文档的行会让归属链看起来"还有文档"，
            # 影响 A-03 的孤儿判定；这里顺手清掉。
            conn.execute(
                "DELETE FROM story_docs WHERE raw_doc_id NOT IN (SELECT id FROM raw_docs)"
            )
        return cur.rowcount

    def prune_content_cache(self, keep_days: int) -> int:
        """正文缓存也要过期：一次低质量抽取否则会被永久复用（BUG-35）。"""
        cutoff = (datetime.now(CST) - timedelta(days=keep_days)).replace(microsecond=0).isoformat()
        with self.tx() as conn:
            cur = conn.execute("DELETE FROM content_cache WHERE fetched_at < ?", (cutoff,))
        return cur.rowcount

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    # ---------- rubric ----------
    def save_rubric(self, version: str, payload: dict[str, Any]) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rubrics (version, payload, created_at) VALUES (?,?,?)",
                (version, json.dumps(payload, ensure_ascii=False), now_iso()),
            )

    # ---------- pushes ----------
    def already_pushed_slot(self, push_date: str, slot: int) -> bool:
        """测试与维护脚本用：判断某天某槽是否已推送。"""
        row = self.conn.execute(
            "SELECT 1 FROM pushes WHERE push_date=? AND slot=? AND delivery_ok=1",
            (push_date, slot),
        ).fetchone()
        return row is not None

    def pushed_today(self, push_date: str) -> bool:
        """当日是否已有任意一条成功送达。

        幂等判定不应该只看槽 1：仅商品日、纯价格日都可能只写槽 5 而不写槽 1
        （核查报告 BUG-22）。
        """
        row = self.conn.execute(
            "SELECT 1 FROM pushes WHERE push_date=? AND delivery_ok=1 LIMIT 1", (push_date,)
        ).fetchone()
        return row is not None

    def pending_today(self, push_date: str) -> bool:
        """当日是否有「发出去但还没确认」的在途记录（delivery_ok=2）。

        进程在"已发送、未写台账"之间被杀时会留下这种行。再跑一次如果当成没发过，
        就会给读者重发一遍；当成已发过，最多漏掉一次（D-03）。
        """
        row = self.conn.execute(
            "SELECT 1 FROM pushes WHERE push_date=? AND delivery_ok=2 LIMIT 1", (push_date,)
        ).fetchone()
        return row is not None

    def begin_push_batch(self, rows: list[dict[str, Any]], sent_at: str) -> str:
        """发送前把这一批写进台账，状态 2（在途）。返回本次批次标识 sent_at。"""
        with self.tx() as conn:
            for push in rows:
                conn.execute(
                    """
                    INSERT INTO pushes (push_date, slot, story_id, sku_ids, title,
                                        importance, channel, payload, sent_at, delivery_ok)
                    VALUES (?,?,?,?,?,?,?,?,?,2)
                    ON CONFLICT(push_date, slot) DO UPDATE SET
                        story_id=excluded.story_id, sku_ids=excluded.sku_ids,
                        title=excluded.title, importance=excluded.importance,
                        channel=excluded.channel, payload=excluded.payload,
                        sent_at=excluded.sent_at,
                        delivery_ok=CASE WHEN pushes.delivery_ok = 1 THEN 1 ELSE 2 END
                    -- 已送达的行是不可变的历史：--force 重推不能把"读者当时收到的那一份"
                    -- 改写成新的 payload / 新的时间（P1-5）
                    WHERE pushes.delivery_ok <> 1
                    """,
                    (
                        push["push_date"], push["slot"], push.get("story_id"),
                        json.dumps(push.get("sku_ids") or [], ensure_ascii=False),
                        push.get("title"), push.get("importance"), push["channel"],
                        push.get("payload"), sent_at,
                    ),
                )
        return sent_at

    def finish_push_batch(self, sent_at: str, ok: bool) -> int:
        """确认这一批的结果：成功 1、失败 0。已成功的行不会被降级（D-11）。"""
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE pushes SET delivery_ok=? WHERE sent_at=? AND delivery_ok=2",
                (1 if ok else 0, sent_at),
            )
        return cur.rowcount

    # 在途超时：超过这个时间还停在 2 的行，判定为「进程中途死掉」
    INFLIGHT_TIMEOUT_MINUTES = 15

    def reconcile_inflight(self, timeout_minutes: int | None = None) -> list[dict[str, Any]]:
        """把卡在「在途」的批次收敛成终态（失败）。

        状态机只有三个终态：1（已送达）/ 0（失败）/ 2（在途）。
        问题在于进程可能在 begin 之后、finish 之前被杀（任务超时、断电），
        这一行就永远停在 2；下一次运行会把它当成「今天已经推过」直接跳过，
        当天静默不推、退出码还是 0。所以启动时必须做一次收敛：

        - 超时的 2 → 0（失败）。当天可以重试，且不再被当成已推送；
        - 返回被收敛的行，调用方负责告警。

        幂等：收敛后不再是 2，重复调用不会重复处理。
        """
        minutes = self.INFLIGHT_TIMEOUT_MINUTES if timeout_minutes is None else timeout_minutes
        cutoff = (datetime.now(CST) - timedelta(minutes=minutes)).isoformat()
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT id, push_date, slot, sent_at FROM pushes"
                " WHERE delivery_ok=2 AND sent_at < ?",
                (cutoff,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE pushes SET delivery_ok=0 WHERE delivery_ok=2 AND sent_at < ?",
                    (cutoff,),
                )
        return [dict(r) for r in rows]

    def set_stories_status(self, story_ids: list[str], status: str) -> int:
        ids = [str(i) for i in story_ids if i]
        if not ids:
            return 0
        with self.tx() as conn:
            cur = conn.execute(
                f"UPDATE stories SET status=? WHERE id IN ({','.join('?' * len(ids))})",
                (status, *ids),
            )
        return cur.rowcount

    def record_push(self, push: dict[str, Any]) -> int:
        """单行写台账（含「已送达不降级」）。生产走 begin/finish_push_batch，
        这里保留给测试与维护脚本模拟历史推送记录。"""
        with self.tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO pushes (push_date, slot, story_id, sku_ids, title,
                                    importance, channel, payload, sent_at, delivery_ok)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(push_date, slot) DO UPDATE SET
                    story_id=excluded.story_id, sku_ids=excluded.sku_ids,
                    title=excluded.title, importance=excluded.importance,
                    payload=excluded.payload, sent_at=excluded.sent_at,
                    -- 已经成功送达的行不得被后来的失败覆盖（--force 重推失败时尤其危险，D-11）
                    delivery_ok = CASE
                        WHEN pushes.delivery_ok = 1 THEN 1
                        ELSE excluded.delivery_ok END
                """,
                (
                    push["push_date"], push["slot"], push.get("story_id"),
                    json.dumps(push.get("sku_ids") or [], ensure_ascii=False),
                    push.get("title"), push.get("importance"), push["channel"],
                    push.get("payload"), push.get("sent_at") or now_iso(),
                    int(push.get("delivery_ok", 1)),
                ),
            )
            return int(cur.lastrowid or 0)

    def pushes_on(self, push_date: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM pushes WHERE push_date=? ORDER BY slot", (push_date,)
            )
        )

    # ---------- interests ----------
    def interest_map(self) -> dict[str, float]:
        rows = self.conn.execute("SELECT key, weight FROM interests")
        return {r["key"]: r["weight"] for r in rows}

    def bump_interest(self, key: str, delta: float) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO interests (key, weight, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    weight = MAX(-1.0, MIN(1.0, interests.weight + excluded.weight)),
                    updated_at = excluded.updated_at
                """,
                (key, delta, now_iso()),
            )

    def claim_feedback_once(self, story_id: str, signal: str, day: str) -> bool:
        """同一 story + 信号每天只允许计一次兴趣变化，防止连点刷权重。"""
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO feedback_dedup (story_id, signal, day) VALUES (?,?,?)",
                (story_id, signal, day),
            )
            return cur.rowcount > 0

    def bump_fake_report(self, source_id: str, delta: int = 1) -> None:
        """记录用户把该源的内容标为「假的」；累计 ≥3 次会在定级时降一级。"""
        with self.tx() as conn:
            conn.execute(
                "UPDATE sources SET fake_reports = fake_reports + ? WHERE id=?",
                (delta, source_id),
            )

    # ---------- runs ----------
    def start_run(self) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now_iso(),)
            )
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any] | None = None,
                   error: str | None = None) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
                (now_iso(), status, json.dumps(stats or {}, ensure_ascii=False), error, run_id),
            )

    def close(self) -> None:
        self.conn.close()
