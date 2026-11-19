# -*- coding: utf-8 -*-
"""F-02 回归测试：feed 没有日期时不得伪造「刚刚发布」。

这条是 P0：造假日期会直接骗过 24h 推送窗口和时效计分。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.adapters import to_iso  # noqa: E402
from infotwitter.db import CST  # noqa: E402


class _Entry(dict):
    """模拟 feedparser 的 entry（支持 .get）。"""


class ToIsoTest(unittest.TestCase):
    def test_no_date_returns_none(self):
        self.assertIsNone(
            to_iso(_Entry({"title": "x"})),
            "没有日期必须返回 None，不能拿当前时间顶替",
        )

    def test_published_parsed_is_used(self):
        struct = (2026, 11, 12, 3, 0, 0, 0, 0, 0)
        got = to_iso(_Entry({"published_parsed": struct}))
        self.assertIsNotNone(got)
        dt = datetime.fromisoformat(got)
        self.assertEqual(dt.astimezone(timezone.utc).date().isoformat(), "2026-11-12")

    def test_explicit_fallback_still_honoured(self):
        fb = datetime(2026, 11, 1, 8, 0, 0, tzinfo=CST)
        self.assertEqual(to_iso(_Entry({}), fallback=fb), "2026-11-01T08:00:00+08:00")

    def test_invented_date_would_be_caught_by_guard(self):
        """没有日期时 time_source 必须是 fetch —— 上游守卫靠它拦旧文。"""
        published = to_iso(_Entry({}))
        time_source = "feed" if published else "fetch"
        self.assertEqual(time_source, "fetch")
        # 守卫条件（pipeline.py）：time_source=='fetch' 且 published 为空 → 不进今日池
        self.assertTrue(time_source == "fetch" and not published)


if __name__ == "__main__":
    unittest.main()
