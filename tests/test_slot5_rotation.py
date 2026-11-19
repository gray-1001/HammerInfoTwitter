# -*- coding: utf-8 -*-
"""槽 5 轮换的回归测试（F-03）。

轮换失效过一次：种子每天 +1 被当成加法常数，取模后只是循环移位，
连续 7 天 top5 完全一样。这里用「7 天的种子必须产出不同的集合」把行为钉住。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.config import load_config
from infotwitter.db import Database
from infotwitter.pricing.summary import build_summary

DB = Path(__file__).resolve().parents[1] / "data" / "infotwitter.db"


@unittest.skipUnless(DB.exists(), "需要生产库做只读回归")
class Slot5RotationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config()
        # 只读打开：这个测试不该有写生产库的能力
        cls.db = Database(DB, read_only=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def _top5(self, seed: int) -> list[str]:
        rows = build_summary(self.db.conn, self.cfg, limit=5, rotate_seed=seed)
        return [r["id"] for r in rows]

    def test_seven_consecutive_days_differ(self):
        days = [20261113 + i for i in range(7)]
        sets = [frozenset(self._top5(d)) for d in days]
        distinct = len(set(sets))
        self.assertGreaterEqual(
            distinct, 3,
            f"连续 7 天的槽 5 只有 {distinct} 种组合，轮换基本没生效：{sets}",
        )

    def test_same_seed_same_result(self):
        a = self._top5(20261119)
        b = self._top5(20261119)
        self.assertEqual(a, b, "同一种子必须产出同一结果（确定性）")


if __name__ == "__main__":
    unittest.main()
