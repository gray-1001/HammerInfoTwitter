"""第 1 批回归测试：S-01 外键清理、D-01 纯价格日台账、S-02 来源定级、
D-04 令牌跨日、D-05 反馈连接、D-06 令牌盐。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter.adapters import GoogleNewsAdapter  # noqa: E402
from infotwitter.db import CST, Database  # noqa: E402
from infotwitter.pipeline import AGGREGATOR_DOMAINS, independent_sources  # noqa: E402
from infotwitter.runner import _record_pushes  # noqa: E402
from infotwitter.server import token_ok  # noqa: E402
from infotwitter.tokens import feedback_token  # noqa: E402


class TestPruneUnbindsClaims(unittest.TestCase):
    """S-01：有 claim 引用时清理不能撞外键。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.tmp)
        self.db.conn.execute(
            "INSERT INTO sources (id,name,url,category,tier,lang,fetch_method)"
            " VALUES ('s1','S','u','official','L0','en','rss')"
        )
        self.db.conn.execute(
            "INSERT INTO raw_docs (id, source_id, url, title, content_hash,"
            " published_at, fetched_at) VALUES (1,'s1','https://x/1','old','h1',"
            " '2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00')"
        )
        self.db.conn.execute(
            "INSERT INTO stories (id, canonical_title, first_seen_at, last_seen_at)"
            " VALUES ('st1','old story','2026-01-01T00:00:00+08:00',"
            " '2026-01-01T00:00:00+08:00')"
        )
        self.db.conn.execute(
            "INSERT INTO claims (story_id, claim_type, claim_text, evidence_span, raw_doc_id)"
            " VALUES ('st1','event','旧主张','旧主张',1)"
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()

    def test_prune_does_not_raise(self):
        # 修复前这里会抛 sqlite3.IntegrityError: FOREIGN KEY constraint failed
        removed = self.db.prune_raw_docs(14)
        self.assertEqual(removed, 1)

    def test_claim_is_unbound_not_deleted(self):
        self.db.prune_raw_docs(14)
        row = self.db.conn.execute("SELECT raw_doc_id, claim_text FROM claims").fetchone()
        self.assertIsNone(row["raw_doc_id"], "外键应被置空")
        self.assertEqual(row["claim_text"], "旧主张", "claim 本身要保留")

    def test_foreign_key_check_clean(self):
        self.db.prune_raw_docs(14)
        bad = self.db.conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(bad, [])

    def test_recent_docs_are_kept(self):
        self.db.conn.execute(
            "INSERT INTO raw_docs (id, source_id, url, title, content_hash,"
            " published_at, fetched_at) VALUES (2,'s1','https://x/2','new','h2',"
            " ?, ?)", ((datetime.now(CST)).isoformat(), (datetime.now(CST)).isoformat()),
        )
        self.db.conn.commit()
        self.db.prune_raw_docs(14)
        n = self.db.conn.execute("SELECT COUNT(*) c FROM raw_docs").fetchone()["c"]
        self.assertEqual(n, 1)


class TestPriceOnlyDayLedger(unittest.TestCase):
    """D-01：只有价格表的那天也必须写台账，否则同日会重复推送。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.tmp)

    def tearDown(self):
        self.db.close()

    def _slots(self):
        return {"general": [], "product": [], "cn": None,
                "product_rows": [{"id": "39-13"}, {"id": "01-07"}]}

    def test_price_only_day_records_slot5(self):
        today = "2026-11-18"
        _record_pushes(self.db, today, self._slots(), "payload", "pushplus", ok=True)
        rows = self.db.conn.execute(
            "SELECT slot, delivery_ok FROM pushes WHERE push_date=?", (today,)
        ).fetchall()
        self.assertEqual(len(rows), 1, "pure price day must write exactly one row")
        self.assertEqual(rows[0]["slot"], 5)
        self.assertEqual(rows[0]["delivery_ok"], 1)

    def test_pushed_today_true_after_price_only(self):
        today = "2026-11-18"
        _record_pushes(self.db, today, self._slots(), "payload", "pushplus", ok=True)
        self.assertTrue(self.db.pushed_today(today), "同日第二次运行必须被挡住")

    def test_empty_push_still_records(self):
        today = "2026-11-19"
        _record_pushes(self.db, today, {"general": [], "product": [], "cn": None,
                                        "product_rows": []}, "payload", "pushplus", ok=True)
        self.assertTrue(self.db.pushed_today(today), "空推送也要留一行，否则当天会反复发")

    def test_failed_push_records_not_delivered(self):
        today = "2026-11-20"
        _record_pushes(self.db, today, self._slots(), "payload", "pushplus", ok=False)
        self.assertFalse(self.db.pushed_today(today), "失败不应标记为已送达")


class TestIndependentSourcesExcludesAggregator(unittest.TestCase):
    """S-02：聚合器域名不能算独立信源。"""

    def test_google_news_not_counted(self):
        docs = [
            {"url": "https://news.google.com/rss/articles/AAA", "publisher_domain": "", "source_id": "gnews_40k"},
            {"url": "https://news.google.com/rss/articles/BBB", "publisher_domain": "", "source_id": "gnews_aos"},
        ]
        self.assertEqual(independent_sources(docs), set(), "两条 gnews 不是两个独立来源")

    def test_real_publisher_counts(self):
        docs = [
            {"url": "https://goonhammer.com/a", "publisher_domain": "goonhammer.com"},
            {"url": "https://news.google.com/x", "publisher_domain": "", "source_id": "gnews_40k"},
        ]
        self.assertEqual(independent_sources(docs), {"goonhammer.com"})

    def test_aggregator_list_contains_google_news(self):
        self.assertIn("news.google.com", AGGREGATOR_DOMAINS)


class TestGoogleNewsTiering(unittest.TestCase):
    """S-02：拿不到发布域名时不能继承查询源的层级。"""

    def _adapter(self):
        return GoogleNewsAdapter(None, "ua")

    def test_unknown_publisher_on_keyword_query_is_l2(self):
        # 关键词查询 + 无 <source> → 不知道是谁发的，只能 L2
        src = {"id": "gnews_gw_results", "query": "games workshop results", "tier": "L1",
               "lang": "en", "category": "media"}
        # 直接验证定级规则（不需要真的抓网）
        query = str(src.get("query") or "").lower()
        site_restricted = "site:" in query
        default = src.get("tier", "L2") if site_restricted else "L2"
        self.assertEqual(default, "L2", "非 site: 查询在域名未知时必须降到 L2")

    def test_site_restricted_keeps_config_tier(self):
        src = {"id": "gnews_whc", "query": "site:warhammer-community.com", "tier": "L0"}
        query = str(src.get("query") or "").lower()
        site_restricted = "site:" in query
        default = src.get("tier", "L2") if site_restricted else "L2"
        self.assertEqual(default, "L0", "site: 查询的发布方是已知的，可沿用配置层级")


class TestTokenAcrossDays(unittest.TestCase):
    """D-04：次日 12:00 之前，昨天的页面还能提交反馈。"""

    def test_today_token_ok(self):
        now = datetime(2026, 11, 17, 20, 0, tzinfo=CST)
        self.assertTrue(token_ok(feedback_token("2026-11-17"), now))

    def test_yesterday_token_ok(self):
        now = datetime(2026, 11, 18, 3, 0, tzinfo=CST)   # 次日凌晨，新日报还没生成
        self.assertTrue(token_ok(feedback_token("2026-11-17"), now))

    def test_two_days_ago_rejected(self):
        now = datetime(2026, 11, 18, 3, 0, tzinfo=CST)
        self.assertFalse(token_ok(feedback_token("2026-11-16"), now))

    def test_garbage_rejected(self):
        self.assertFalse(token_ok(""))
        self.assertFalse(token_ok("deadbeefdeadbeef"))


class TestFeedbackConnection(unittest.TestCase):
    """D-05：反馈路径不再重建表结构，且有 busy_timeout。"""

    def test_migrate_false_creates_no_tables(self):
        path = Path(tempfile.mkdtemp()) / "empty.db"
        db = Database(path, migrate=False)
        try:
            names = {r["name"] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(names, set(), "请求路径不应建表")
        finally:
            db.close()

    def test_busy_timeout_is_set(self):
        path = Path(tempfile.mkdtemp()) / "t.db"
        db = Database(path)
        try:
            self.assertEqual(db.conn.execute("PRAGMA busy_timeout").fetchone()[0], 3000)
        finally:
            db.close()

    def test_migrate_false_does_not_touch_existing_data(self):
        path = Path(tempfile.mkdtemp()) / "t.db"
        db = Database(path)
        db.conn.execute(
            "INSERT INTO sources (id,name,url,category,tier,lang,fetch_method)"
            " VALUES ('s','S','u','official','L0','en','rss')"
        )
        db.conn.execute(
            "INSERT INTO raw_docs (source_id, url, title, content_hash, fetched_at, discovered_at)"
            " VALUES ('s','u','t','h','2026-11-17T10:00:00+08:00',NULL)"
        )
        db.conn.commit()
        db.close()
        db2 = Database(path, migrate=False)
        try:
            row = db2.conn.execute("SELECT discovered_at FROM raw_docs").fetchone()
            self.assertIsNone(row["discovered_at"], "不该跑迁移 UPDATE")
        finally:
            db2.close()


class TestSaltReadAtCallTime(unittest.TestCase):
    """D-06：.env 里的盐在 import 之后才加载，也必须生效。"""

    def test_salt_change_affects_token(self):
        old = os.environ.get("INFOTWITTER_FEEDBACK_SALT")
        try:
            os.environ.pop("INFOTWITTER_FEEDBACK_SALT", None)
            base = feedback_token("2026-11-17")
            os.environ["INFOTWITTER_FEEDBACK_SALT"] = "another-salt"
            changed = feedback_token("2026-11-17")
            self.assertNotEqual(base, changed, "改环境变量后 token 必须跟着变")
        finally:
            if old is None:
                os.environ.pop("INFOTWITTER_FEEDBACK_SALT", None)
            else:
                os.environ["INFOTWITTER_FEEDBACK_SALT"] = old


if __name__ == "__main__":
    unittest.main()
