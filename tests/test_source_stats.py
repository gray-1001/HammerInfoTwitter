"""M2 调整后的回归测试：源统计、Reddit 时间解析、内容优先级。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.adapters.reddit import _parse_ts  # noqa: E402
from infotwitter.db import CST, Database  # noqa: E402


class TestSourceStats(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "s.db"
        self.db = Database(self.tmp)

    def tearDown(self):
        self.db.close()

    def test_record_and_summarize(self):
        today = datetime.now(CST).date().isoformat()
        self.db.record_source_stats(today, {
            "src_a": (10, True, 500),
            "src_b": (0, False, 0),
            "src_c": (3, True, 100),
        })
        rows = {r["source_id"]: r for r in self.db.source_stats_summary(7)}
        self.assertEqual(rows["src_a"]["docs"], 10)
        self.assertEqual(rows["src_a"]["fails"], 0)
        self.assertEqual(rows["src_a"]["avg_body"], 500)
        self.assertEqual(rows["src_b"]["fails"], 1)
        self.assertEqual(rows["src_c"]["docs"], 3)

    def test_rerun_same_day_updates_not_duplicates(self):
        today = datetime.now(CST).date().isoformat()
        self.db.record_source_stats(today, {"src_a": (5, True, 300)})
        self.db.record_source_stats(today, {"src_a": (8, True, 400)})
        rows = self.db.source_stats_summary(7)
        self.assertEqual(len(rows), 1, "同一天同一源只应有一行")
        self.assertEqual(rows[0]["docs"], 8)

    def test_old_rows_excluded(self):
        old = (datetime.now(CST) - timedelta(days=30)).date().isoformat()
        self.db.record_source_stats(old, {"src_old": (99, True, 999)})
        self.assertEqual(self.db.source_stats_summary(7), [])


class TestRedditTimestamp(unittest.TestCase):
    def test_iso_with_z(self):
        dt = _parse_ts("2026-11-18T05:13:02.340Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_faceplate_offset_format(self):
        """faceplate-timeago 用 +0000 而不是 +00:00。"""
        dt = _parse_ts("2026-11-18T05:13:02.340000+0000")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.astimezone(CST).hour, 13)

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_ts(""))
        self.assertIsNone(_parse_ts("3 hours ago"))
        self.assertIsNone(_parse_ts(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
