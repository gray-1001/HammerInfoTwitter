# -*- coding: utf-8 -*-
"""状态机终态与恢复的回归测试（P-01）。

场景：进程在 begin_push_batch 之后、finish_push_batch 之前被杀。
期望：下一次运行能把卡住的「在途(2)」收敛成「失败(0)」，当天可以重推。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.db import CST, Database


def _row(sent_at: str, ok: int = 2) -> dict:
    return {
        "push_date": sent_at[:10],
        "slot": 1,
        "story_id": None,   # pushes.story_id 有外键，测试里用 NULL 避免造 story
        "sku_ids": [],
        "title": "t",
        "importance": 50.0,
        "channel": "pushplus",
        "payload": "<p>x</p>",
        "sent_at": sent_at,
    }


class ReconcileInflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _state(self) -> int:
        return self.db.conn.execute("SELECT delivery_ok FROM pushes").fetchone()[0]

    def test_stale_inflight_becomes_failed(self):
        old = (datetime.now(CST) - timedelta(hours=3)).replace(microsecond=0).isoformat()
        self.db.begin_push_batch([_row(old, 2)], old)
        self.assertEqual(self._state(), 2, "begin 之后应为在途状态 2")

        stale = self.db.reconcile_inflight(timeout_minutes=15)
        self.assertEqual(len(stale), 1, "超时的在途行应被收敛")
        self.assertEqual(self._state(), 0, "收敛后应为失败 0，当天才允许重试")

    def test_fresh_inflight_untouched(self):
        now = datetime.now(CST).replace(microsecond=0).isoformat()
        self.db.begin_push_batch([_row(now, 2)], now)
        stale = self.db.reconcile_inflight(timeout_minutes=15)
        self.assertEqual(stale, [], "刚发出的在途行不能被误判为失败")
        self.assertEqual(self._state(), 2)

    def test_delivered_never_downgraded(self):
        old = (datetime.now(CST) - timedelta(hours=3)).replace(microsecond=0).isoformat()
        self.db.begin_push_batch([_row(old, 2)], old)
        self.db.finish_push_batch(old, True)
        self.assertEqual(self._state(), 1)
        self.db.reconcile_inflight(timeout_minutes=15)
        self.assertEqual(self._state(), 1, "已送达的行是历史，任何收敛都不能改它")

    def test_reconcile_is_idempotent(self):
        old = (datetime.now(CST) - timedelta(hours=3)).replace(microsecond=0).isoformat()
        self.db.begin_push_batch([_row(old, 2)], old)
        self.assertEqual(len(self.db.reconcile_inflight(15)), 1)
        self.assertEqual(len(self.db.reconcile_inflight(15)), 0, "第二次不应重复处理")


if __name__ == "__main__":
    unittest.main()
