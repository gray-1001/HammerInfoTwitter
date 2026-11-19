"""P0 回归测试：story 身份确定性、孤儿清理、backlog 过滤。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.db import Database, now_iso  # noqa: E402
from infotwitter.pipeline import assign_stable_ids, story_id_for  # noqa: E402


def cluster(title: str, members: list[int], first_seen: str) -> dict:
    return {
        "id": story_id_for(title, first_seen),
        "canonical_title": title,
        "first_seen_at": first_seen,
        "member_ids": members,
    }


class TestStableIds(unittest.TestCase):
    """salt 必须由簇成员决定，不能依赖遍历顺序。"""

    def test_same_members_same_id_regardless_of_order(self):
        a = cluster("分家", [1859, 1891], "2026-11-17T20:42:00+08:00")
        b = cluster("分家", [1900], "2026-11-17T20:42:00+08:00")

        order1 = [dict(a), dict(b)]
        assign_stable_ids(order1, {}, {})
        ids1 = {s["member_ids"][0]: s["id"] for s in order1}

        order2 = [dict(b), dict(a)]
        assign_stable_ids(order2, {}, {})
        ids2 = {s["member_ids"][0]: s["id"] for s in order2}

        self.assertEqual(ids1[1859], ids2[1859], "同一簇换顺序后 id 必须一致")
        self.assertEqual(ids1[1900], ids2[1900])
        self.assertNotEqual(ids1[1859], ids1[1900], "两个簇不能共用同一个 id")

    def test_repeat_calls_are_stable(self):
        """同一批输入跑两次，id 完全一致（可复现）。"""
        runs = []
        for _ in range(2):
            stories = [
                cluster("分家", [1, 2], "2026-11-17T20:42:00+08:00"),
                cluster("分家", [3], "2026-11-17T20:42:00+08:00"),
                cluster("分家", [4], "2026-11-17T20:42:00+08:00"),
            ]
            assign_stable_ids(stories, {}, {})
            runs.append(sorted(s["id"] for s in stories))
        self.assertEqual(runs[0], runs[1])


class TestOrphanHandling(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.tmp)

    def tearDown(self):
        self.db.close()

    def _add_story(self, sid: str, score: float, docs: list[int] | None = None):
        self.db.conn.execute(
            "INSERT INTO stories (id, canonical_title, first_seen_at, last_seen_at, "
            "importance_total, status) VALUES (?,?,?,?,?, 'scored')",
            (sid, f"title-{sid}", now_iso(), now_iso(), score),
        )
        for d in docs or []:
            self.db.conn.execute(
                "INSERT INTO story_docs (raw_doc_id, story_id, updated_at) VALUES (?,?,?)",
                (d, sid, now_iso()),
            )
        self.db.conn.commit()

    def test_backlog_excludes_orphans(self):
        self._add_story("with_docs", 80, docs=[101])
        self._add_story("orphan", 90)
        rows = self.db.backlog_candidates(days=7, min_score=45, exclude=set())
        ids = {r["id"] for r in rows}
        self.assertIn("with_docs", ids)
        self.assertNotIn("orphan", ids, "孤儿 story 不得进入 backlog 候选")

    def test_prune_removes_orphans_only(self):
        self._add_story("keep", 70, docs=[201])
        self._add_story("orphan", 60)
        # 把时间调到两天前，绕过 keep_days=1 的保护
        self.db.conn.execute("UPDATE stories SET first_seen_at='2026-11-16T00:00:00+08:00'")
        self.db.conn.commit()

        removed = self.db.prune_orphan_stories(keep_days=1)
        self.assertEqual(removed, 1)
        left = {r["id"] for r in self.db.conn.execute("SELECT id FROM stories")}
        self.assertEqual(left, {"keep"})

    def test_prune_keeps_recent_orphans(self):
        """刚建的孤儿不删：本轮可能还没绑定文档。"""
        self._add_story("fresh_orphan", 60)
        self.assertEqual(self.db.prune_orphan_stories(keep_days=1), 0)

    def test_prune_never_deletes_pushed(self):
        self._add_story("pushed_orphan", 60)
        self.db.conn.execute("UPDATE stories SET first_seen_at='2026-11-16T00:00:00+08:00'")
        self.db.conn.execute(
            "INSERT INTO pushes (push_date, slot, story_id, title, importance, channel, "
            "sent_at, delivery_ok) VALUES ('2026-11-16', 1, 'pushed_orphan', 'x', 60, 'test', ?, 1)",
            (now_iso(),)
        )
        self.db.conn.commit()
        self.assertEqual(self.db.prune_orphan_stories(keep_days=1), 0)
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) c FROM stories").fetchone()["c"], 1
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
