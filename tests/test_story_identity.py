"""事件身份稳定性（A-06）与孤儿回收（A-03）的回归测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter.db import CST, Database  # noqa: E402
from infotwitter.pipeline import assign_stable_ids, cluster_docs, story_id_for  # noqa: E402


def _doc(doc_id, title, published, source="s1", tier="L0"):
    return {
        "id": doc_id, "source_id": source, "title": title, "url": f"https://x/{doc_id}",
        "published_at": published, "fetched_at": published, "discovered_at": published,
        "tier": tier, "lang": "en", "body": "", "analysis": None,
        "publisher_domain": "example.com",
    }


class TestStableStoryId(unittest.TestCase):
    """同一事件不能因为簇里多了一篇更早的文章就换 id。"""

    T = "Legions of Khemri arise in the old world"

    def test_id_would_drift_without_the_link(self):
        """先证明问题存在：不加归属链时，多一篇更早的文档 id 就变了。"""
        a = cluster_docs([_doc(1, self.T, "2026-11-16T10:00:00+08:00")], {})
        b = cluster_docs(
            [
                _doc(1, self.T, "2026-11-16T10:00:00+08:00"),
                _doc(2, self.T, "2026-11-15T10:00:00+08:00"),
            ],
            {},
        )
        self.assertNotEqual(a[0]["id"], b[0]["id"], "这正是要修的现象")

    def test_link_keeps_id_stable(self):
        a = cluster_docs([_doc(1, self.T, "2026-11-16T10:00:00+08:00")], {})
        original = a[0]["id"]

        b = cluster_docs(
            [
                _doc(1, self.T, "2026-11-16T10:00:00+08:00"),
                _doc(2, self.T, "2026-11-15T10:00:00+08:00"),
            ],
            {},
            doc_to_story={1: original},
            story_first_seen={original: "2026-11-16T10:00:00+08:00"},
        )
        self.assertEqual(b[0]["id"], original, "有归属链时应复用原 id")

    def test_merge_prefers_older_story(self):
        a_old = cluster_docs([_doc(1, self.T, "2026-11-10T10:00:00+08:00")], {})
        b_new = cluster_docs([_doc(2, self.T, "2026-11-16T10:00:00+08:00")], {})
        first_seen = {
            a_old[0]["id"]: "2026-11-10T10:00:00+08:00",
            b_new[0]["id"]: "2026-11-16T10:00:00+08:00",
        }
        merged = cluster_docs(
            [
                _doc(1, self.T, "2026-11-10T10:00:00+08:00"),
                _doc(2, self.T, "2026-11-16T10:00:00+08:00"),
            ],
            {},
            doc_to_story={1: a_old[0]["id"], 2: b_new[0]["id"]},
            story_first_seen=first_seen,
        )
        self.assertEqual(merged[0]["id"], a_old[0]["id"], "合并时保留更老的事件 id")

    def test_split_does_not_reuse_same_id_twice(self):
        """拆分时两个簇都想复用同一个旧 id：只能有一个成功。"""
        shared = "sharedid12345678"
        stories = [
            {"id": story_id_for("A", "2026-11-10"), "canonical_title": "A",
             "first_seen_at": "2026-11-10T10:00:00+08:00", "member_ids": [1]},
            {"id": story_id_for("B", "2026-11-11"), "canonical_title": "B",
             "first_seen_at": "2026-11-11T10:00:00+08:00", "member_ids": [2]},
        ]
        assign_stable_ids(stories, {1: shared, 2: shared}, {shared: "2026-11-10T10:00:00+08:00"})
        ids = [s["id"] for s in stories]
        self.assertEqual(len(set(ids)), 2, "两个簇不能拿同一个 id")
        self.assertIn(shared, ids, "老事件应保留原 id")


class TestStoryDocsLink(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.tmp)

    def tearDown(self):
        self.db.close()

    def test_link_and_map_roundtrip(self):
        self.db.link_story_docs("st1", [1, 2, 3])
        self.assertEqual(self.db.doc_story_map(), {1: "st1", 2: "st1", 3: "st1"})

    def test_relinking_moves_doc(self):
        self.db.link_story_docs("st1", [1, 2])
        self.db.link_story_docs("st2", [2])
        self.assertEqual(self.db.doc_story_map(), {1: "st1", 2: "st2"})

    def test_empty_ids_is_noop(self):
        self.assertEqual(self.db.link_story_docs("st1", []), 0)


class TestOrphanReclaim(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.tmp)

    def tearDown(self):
        self.db.close()

    def _story(self, sid, hours_ago, status="scored"):
        t = (datetime.now(CST) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
        self.db.conn.execute(
            "INSERT INTO stories (id, canonical_title, first_seen_at, last_seen_at, status)"
            " VALUES (?,?,?,?,?)",
            (sid, sid, t, t, status),
        )
        self.db.conn.commit()

    def _status(self, sid):
        return self.db.conn.execute(
            "SELECT status FROM stories WHERE id=?", (sid,)
        ).fetchone()["status"]

    def test_absent_and_old_is_expired(self):
        self._story("old", hours_ago=48)
        self.assertEqual(self.db.expire_absent_stories({"fresh"}, keep_days=1), 1)
        self.assertEqual(self._status("old"), "expired")

    def test_absent_but_recent_is_kept(self):
        self._story("recent", hours_ago=2)
        # 它还有挂靠文档，且没超过宽限期 → 先观察
        self.db.link_story_docs("recent", [1])
        self.assertEqual(self.db.expire_absent_stories({"fresh"}, keep_days=1), 0)
        self.assertEqual(self._status("recent"), "scored")

    def test_merged_away_story_expires_immediately(self):
        """文档全被别的事件收走（合并）→ 不必等宽限期，立即回收。"""
        self._story("merged", hours_ago=1)
        self.db.link_story_docs("survivor", [1, 2])   # 它的文档已改挂到 survivor
        self.assertEqual(self.db.expire_absent_stories({"survivor"}, keep_days=1), 1)
        self.assertEqual(self._status("merged"), "expired")

    def test_seen_story_is_kept(self):
        self._story("seen", hours_ago=48)
        self.assertEqual(self.db.expire_absent_stories({"seen"}, keep_days=1), 0)
        self.assertEqual(self._status("seen"), "scored")

    def test_empty_run_never_expires(self):
        self._story("old", hours_ago=100)
        self.assertEqual(self.db.expire_absent_stories(set(), keep_days=1), 0)
        self.assertEqual(self._status("old"), "scored", "空运行（采集全挂）不得清库")

    def test_pushed_is_not_expired(self):
        self._story("pushed1", hours_ago=100, status="pushed")
        self.assertEqual(self.db.expire_absent_stories({"other"}, keep_days=1), 0)
        self.assertEqual(self._status("pushed1"), "pushed")

    def test_reappearing_story_is_revived(self):
        self._story("back", hours_ago=48)
        self.db.expire_absent_stories({"other"}, keep_days=1)
        self.assertEqual(self._status("back"), "expired")

        # 下一轮又产出了同一个事件
        self.db.insert_story({
            "id": "back", "canonical_title": "back",
            "first_seen_at": "2026-11-17T10:00:00+08:00", "status": "scored",
        })
        self.assertEqual(self._status("back"), "scored", "回收后再次出现必须复活")

    def test_pushed_stays_pushed_on_upsert(self):
        self._story("p1", hours_ago=1, status="pushed")
        self.db.insert_story({
            "id": "p1", "canonical_title": "p1",
            "first_seen_at": "2026-11-17T10:00:00+08:00", "status": "scored",
        })
        self.assertEqual(self._status("p1"), "pushed")

    def test_backlog_excludes_expired(self):
        self._story("old", hours_ago=48)
        self.db.expire_absent_stories({"other"}, keep_days=1)
        cands = self.db.backlog_candidates(7, 0.0, set())
        self.assertNotIn("old", [c["id"] for c in cands], "回收后不应再被 backlog 捞回")


if __name__ == "__main__":
    unittest.main()
