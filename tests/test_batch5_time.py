"""第 0/1 批回归测试：日期来源、时间口径、事件身份、backlog 后门。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.content import extract_date_published  # noqa: E402
from infotwitter.db import CST, Database  # noqa: E402
from infotwitter.pipeline import (  # noqa: E402
    assign_stable_ids,
    cluster_docs,
    merge_same_title,
    select_slots,
    token_set,
)
from test_pipeline import make_config, make_story  # noqa: E402


class TestJsonLdDate(unittest.TestCase):
    """P0-1：文章页 JSON-LD 的 datePublished 是日期的金标准。"""

    def test_iso_with_timezone(self):
        got = extract_date_published('{"datePublished": "2026-11-15T10:00:00+00:00"}')
        self.assertEqual(got[:10], "2026-11-15")
        self.assertTrue(got.endswith("+08:00"))

    def test_date_only(self):
        self.assertEqual(extract_date_published('{"datePublished":"2026-10-28"}')[:10], "2026-10-28")

    def test_missing_or_invalid(self):
        self.assertEqual(extract_date_published("<html></html>"), "")
        self.assertEqual(extract_date_published('{"datePublished": "nope"}'), "")


class TestCjkTokenSet(unittest.TestCase):
    """P1-4：两字中文标题不能得到空词集（否则永远不聚类）。"""

    def test_short_cjk_title_has_tokens(self):
        self.assertTrue(token_set("分家"), "「分家」必须有词")

    def test_same_short_title_clusters(self):
        docs = [
            {"id": 1, "source_id": "a", "title": "分家", "url": "https://x/1",
             "tier": "L2", "published_at": "2026-11-17T10:00:00+08:00",
             "fetched_at": "2026-11-17T10:00:00+08:00", "discovered_at": "2026-11-17T10:00:00+08:00"},
            {"id": 2, "source_id": "b", "title": "分家", "url": "https://x/2",
             "tier": "L2", "published_at": "2026-11-17T11:00:00+08:00",
             "fetched_at": "2026-11-17T11:00:00+08:00", "discovered_at": "2026-11-17T11:00:00+08:00"},
        ]
        self.assertEqual(len(cluster_docs(docs, {})), 1, "同标题的两篇必须聚成一簇")


class TestStableIdsDeterministic(unittest.TestCase):
    """P1-4：同样的输入，换个顺序不能换 id。"""

    def _stories(self):
        return [
            {"id": "s1", "canonical_title": "A", "first_seen_at": "2026-11-17T10:00:00+08:00",
             "member_ids": [1], "importance_total": 60},
            {"id": "s2", "canonical_title": "B", "first_seen_at": "2026-11-17T10:00:00+08:00",
             "member_ids": [2], "importance_total": 50},
        ]

    def test_order_does_not_change_ids(self):
        a = self._stories()
        b = list(reversed(self._stories()))
        link = {1: "histA", 2: "histB"}
        first_seen = {"histA": "2026-11-01T00:00:00+08:00", "histB": "2026-11-01T00:00:00+08:00"}
        assign_stable_ids(a, link, first_seen)
        assign_stable_ids(b, link, first_seen)
        self.assertEqual({s["canonical_title"]: s["id"] for s in a},
                         {s["canonical_title"]: s["id"] for s in b},
                         "同 first_seen 时也要有确定的第二排序键")


class TestMergePrefersPushed(unittest.TestCase):
    """P1-4：合并要保住已推送的 id，否则去重键被换掉，明天会重推。"""

    def test_keeps_pushed_id(self):
        old = make_story(id="old", title="Sunday Preview", score=50,
                         first_seen_at="2026-11-10T10:00:00+08:00")
        new = make_story(id="new", title="Sunday Preview", score=70,
                         first_seen_at="2026-11-17T10:00:00+08:00")
        kept, merged = merge_same_title([old, new], pushed_ids={"old"})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["id"], "old", "已推送的 id 必须保留")
        self.assertIn("new", merged)

    def test_without_pushed_prefers_earliest(self):
        old = make_story(id="old", title="Sunday Preview", score=50,
                         first_seen_at="2026-11-10T10:00:00+08:00")
        new = make_story(id="new", title="Sunday Preview", score=70,
                         first_seen_at="2026-11-17T10:00:00+08:00")
        kept, _ = merge_same_title([old, new])
        self.assertEqual(kept[0]["id"], "old", "没有台账时保留最早出现的那条")


class TestTierByFirstSeen(unittest.TestCase):
    """P1-2：一级/二级按「首次出现」，不按文章发布日期。"""

    def setUp(self):
        self.cfg = make_config(**{
            "windows.collect_hours": 168,
            "windows.push_hours": 48,
            "thresholds.aged_min_score": 50,
        })
        now = datetime.now(CST).replace(microsecond=0)
        self.now = now

    def _story(self, **kw):
        return make_story(**kw)

    def test_late_discovery_is_fresh(self):
        """文章 3 天前发布，但今天才发现 → 一级（设计 §20）。"""
        now = self.now
        published = (now - timedelta(hours=72)).isoformat()
        discovered = (now - timedelta(hours=1)).isoformat()
        s = self._story(id="late", title="Late discovered breaking news", score=70,
                        published_at=published, first_seen_at=discovered)
        slots = select_slots([s], self.cfg, set(), [])
        self.assertEqual(len(slots["general"]), 1)
        self.assertEqual(slots["general"][0]["_slot_reason"], "qualified",
                         "刚发现的新闻不该被降成「本周稍早」")

    def test_old_article_never_beats_fresh_one(self):
        """文章龄超过一周：可以补位，但不能抢今日头条（A-02 的契约）。

        2026-11-19 调整：原来是「超过一周直接丢」，导致延迟发现的高分老文
        永远推不出去（实测当日最高分 69.0/68.4/64.6 三条被静默丢弃）。
        现在改为进 aged 补位池——有人气更高的新文时轮不到它。
        """
        now = self.now
        published = (now - timedelta(days=10)).isoformat()
        discovered = (now - timedelta(hours=1)).isoformat()
        stale = self._story(id="stale", title="Ten day old article", score=80,
                            published_at=published, first_seen_at=discovered)
        fresh = self._story(id="fresh-one", title="Brand new announcement", score=60,
                            published_at=(now - timedelta(hours=2)).isoformat(),
                            first_seen_at=(now - timedelta(hours=1)).isoformat())
        slots = select_slots([stale, fresh], self.cfg, set(), [])
        reasons = {x["id"]: x.get("_slot_reason") for x in slots["general"]}
        # 旧文只能以 aged（补位）身份出现；新文走正常/二级阈值
        self.assertEqual(reasons.get("stale"), "aged")
        self.assertIn(reasons.get("fresh-one"), {"qualified", "secondary"})

    def test_old_article_excluded_when_slots_full(self):
        """槽位被合格新文占满时，旧文完全不该出现。"""
        now = self.now
        fresh_docs = [
            self._story(id=f"f{i}", title=f"Fresh headline number {i}", score=60,
                        published_at=(now - timedelta(hours=2)).isoformat(),
                        first_seen_at=(now - timedelta(hours=1)).isoformat())
            for i in range(6)
        ]
        stale = self._story(id="stale", title="Ten day old article", score=80,
                            published_at=(now - timedelta(days=10)).isoformat(),
                            first_seen_at=(now - timedelta(hours=1)).isoformat())
        slots = select_slots([stale, *fresh_docs], self.cfg, set(), [])
        ids = [x["id"] for x in slots["general"]]
        self.assertNotIn("stale", ids, "槽位够用时旧文不该占位")

    def test_old_article_can_fill_when_nothing_else(self):
        """没有别的候选时，旧文可以补位（但要标成 aged，不是 qualified）。"""
        now = self.now
        s = self._story(id="stale", title="Ten day old article", score=80,
                        published_at=(now - timedelta(days=10)).isoformat(),
                        first_seen_at=(now - timedelta(hours=1)).isoformat())
        slots = select_slots([s], self.cfg, set(), [])
        ids = [x["id"] for x in slots["general"]]
        self.assertEqual(ids, ["stale"])
        self.assertEqual(slots["general"][0].get("_slot_reason"), "aged")


class TestBacklogNoUndatedBackdoor(unittest.TestCase):
    """P3-2：时间未知的事件不能从 backlog 绕回推送。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")

    def tearDown(self):
        self.db.close()

    def _story(self, sid, time_source, published):
        t = (datetime.now(CST) - timedelta(hours=2)).replace(microsecond=0).isoformat()
        self.db.conn.execute(
            "INSERT INTO stories (id, canonical_title, first_seen_at, last_seen_at,"
            " status, importance_total, time_source, published_at)"
            " VALUES (?,?,?,?,'scored',90,?,?)",
            (sid, sid, t, t, time_source, published),
        )
        self.db.conn.commit()
        self.db.link_story_docs(sid, [abs(hash(sid)) % 100000])

    def test_undated_excluded_from_backlog(self):
        self._story("undated", "fetch", None)
        self.assertEqual(self.db.backlog_candidates(14, 45.0, set()), [])

    def test_dated_included(self):
        self._story("dated", "article", "2026-11-17T00:00:00+08:00")
        self.assertEqual(len(self.db.backlog_candidates(14, 45.0, set())), 1)


class TestPruneUsesDiscoveredAt(unittest.TestCase):
    """P1-3：文章旧但今天才抓到的文档，不能被立刻删掉。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        self.db.conn.execute(
            "INSERT INTO sources (id,name,url,category,tier,lang,fetch_method)"
            " VALUES ('s','S','u','official','L0','en','rss')"
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()

    def test_late_arrival_survives_prune(self):
        now = datetime.now(CST).replace(microsecond=0)
        self.db.conn.execute(
            "INSERT INTO raw_docs (id, source_id, url, title, content_hash,"
            " published_at, fetched_at, discovered_at) VALUES"
            " (1,'s','https://x/1','late','h1',?,?,?)",
            ((now - timedelta(days=20)).isoformat(), now.isoformat(), now.isoformat()),
        )
        self.db.conn.commit()
        removed = self.db.prune_raw_docs(14)
        self.assertEqual(removed, 0, "按入库时间算，今天抓到的不能被删")

    def test_old_document_still_pruned(self):
        now = datetime.now(CST).replace(microsecond=0)
        old = (now - timedelta(days=20)).isoformat()
        self.db.conn.execute(
            "INSERT INTO raw_docs (id, source_id, url, title, content_hash,"
            " published_at, fetched_at, discovered_at) VALUES"
            " (2,'s','https://x/2','old','h2',?,?,?)", (old, old, old),
        )
        self.db.conn.commit()
        self.assertEqual(self.db.prune_raw_docs(14), 1)


if __name__ == "__main__":
    unittest.main()
