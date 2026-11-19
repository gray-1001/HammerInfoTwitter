"""降噪与分析限额的回归测试（C-21 / C-22）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter.db import Database  # noqa: E402


class TestBilibiliTopByPlay(unittest.TestCase):
    """C-21：B 站按播放量取前 N，保持发布时间倒序。"""

    def _docs(self):
        return [
            {"title": f"v{i}", "engagement": play, "published_at": f"2026-11-{10 + i:02d}T00:00:00"}
            for i, play in enumerate([50, 900, 10, 3000, 200, 1200])
        ]

    def test_keeps_top_n_by_play(self):
        from infotwitter.adapters.bilibili import _top_by_play

        kept = _top_by_play(self._docs(), 3)
        # 选的是播放量最高的三条；返回顺序按发布时间倒序（供下游使用）
        self.assertEqual(sorted((d["engagement"] for d in kept), reverse=True), [3000, 1200, 900],
                         "应保留播放量最高的三条")
        self.assertEqual([d["published_at"] for d in kept],
                         sorted((d["published_at"] for d in kept), reverse=True),
                         "返回时按发布时间倒序")
        self.assertEqual([d["published_at"] for d in kept],
                         sorted((d["published_at"] for d in kept), reverse=True))

    def test_zero_keeps_all(self):
        from infotwitter.adapters.bilibili import _top_by_play

        self.assertEqual(len(_top_by_play(self._docs(), 0)), 6)

    def test_fewer_than_cap_unchanged(self):
        from infotwitter.adapters.bilibili import _top_by_play

        self.assertEqual(len(_top_by_play(self._docs(), 10)), 6)


class TestTitleOnlyCap(unittest.TestCase):
    """C-22：标题型文档按源限额，正文够长的不受限。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")

    def tearDown(self):
        self.db.close()

    def _row(self, rid, sid, body):
        return {"id": rid, "source_id": sid, "body": body, "analyzed_at": None}

    def test_caps_per_source(self):
        rows = [self._row(i, "reddit_40k", "短标题") for i in range(10)]
        rows += [self._row(100 + i, "bilibili_wh", "短标题") for i in range(10)]
        kept = self.db.limit_title_only_docs(rows, per_source=3, min_body=400)
        self.assertEqual(len(kept), 6, "每个源只留 3 条")
        self.assertEqual(sum(1 for r in kept if r["source_id"] == "reddit_40k"), 3)

    def test_long_body_not_capped(self):
        rows = [self._row(i, "goonhammer", "正文" * 500) for i in range(10)]
        kept = self.db.limit_title_only_docs(rows, per_source=3, min_body=400)
        self.assertEqual(len(kept), 10, "有正文的文档不受限额")

    def test_zero_means_unlimited(self):
        rows = [self._row(i, "reddit_40k", "短") for i in range(10)]
        self.assertEqual(len(self.db.limit_title_only_docs(rows, per_source=0)), 10)

    def test_keeps_newest_first(self):
        rows = [self._row(i, "reddit_40k", "短") for i in range(5)]
        kept = self.db.limit_title_only_docs(rows, per_source=2)
        self.assertEqual([r["id"] for r in kept], [0, 1], "按传入顺序（发现时间倒序）保留最新的")


if __name__ == "__main__":
    unittest.main()
