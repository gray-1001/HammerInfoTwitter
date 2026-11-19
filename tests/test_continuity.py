"""连续多日运行的集成测试。

目标验收标准之一是「连续产出可用日报」。单次运行无法验证跨天的行为，
这里用可控的时间戳模拟连续三天，检查：

1. 同一天重复运行不重复推送
2. 前一天推送过的内容第二天不再出现
3. 超过 24h 窗口的旧内容不会被当成新闻
4. 没有新内容时用历史 backlog 补位，且标注来源
5. 长期无内容时如实少推，而不是硬凑
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.db import CST  # noqa: E402
from infotwitter.pipeline import select_slots  # noqa: E402
from test_pipeline import make_config, make_story  # noqa: E402


def story_at(hours_ago: float, **kw):
    """构造一条「N 小时前首次出现」的事件。"""
    t = (datetime.now(CST) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
    return make_story(first_seen_at=t, **kw)


class TestContinuity(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()

    def test_same_day_rerun_does_not_repeat(self):
        """同一天跑第二次：已推送的不再入选。"""
        day1 = [story_at(2, id=f"d1s{i}", score=70 - i) for i in range(6)]

        first = select_slots(day1, self.cfg, set(), [])
        self.assertEqual(len(first["general"]), 4)
        pushed = {s["id"] for s in first["general"]}

        second = select_slots(day1, self.cfg, pushed, [])
        overlap = {s["id"] for s in second["general"]} & pushed
        self.assertEqual(overlap, set(), "同一天重跑不得重复推送已发内容")
        # 剩余 2 条仍有效，应补上
        self.assertEqual(len(second["general"]), 2)

    def test_next_day_excludes_pushed_and_expired(self):
        """第二天：昨天推过的锁定，昨天没推的也已过 24h 窗口。"""
        day1 = [story_at(2, id=f"d1s{i}", score=70 - i) for i in range(6)]
        first = select_slots(day1, self.cfg, set(), [])
        pushed = {s["id"] for s in first["general"]}

        # 到了第二天，昨天的事都变成 26 小时前；同时有 4 条新事件
        aged = [story_at(26, id=f"d1s{i}", score=70 - i) for i in range(6)]
        fresh = [story_at(1, id=f"d2s{i}", score=65 - i) for i in range(4)]

        second = select_slots(aged + fresh, self.cfg, pushed, [])
        ids = {s["id"] for s in second["general"]}
        self.assertEqual(ids, {f"d2s{i}" for i in range(4)})
        self.assertEqual(second["mode"], "normal")

    def test_quiet_day_falls_back_to_backlog(self):
        """今天没新闻：用历史未推送内容补位，并标记为历史推荐。"""
        backlog = [
            make_story(id="old1", title="Old but unpushed", score=62,
                       first_seen_at=(datetime.now(CST) - timedelta(days=2)).isoformat()),
        ]
        slots = select_slots([], self.cfg, set(), backlog)
        self.assertEqual(len(slots["general"]), 1)
        self.assertEqual(slots["general"][0]["_slot_reason"], "backlog")
        self.assertIn(slots["mode"], ("backlog_only", "backlog_fill"))

    def test_backlog_decays_with_age(self):
        """历史补位按天数衰减，越旧越靠后。"""
        recent = make_story(id="r", title="Recent backlog", score=60,
                            first_seen_at=(datetime.now(CST) - timedelta(days=1)).isoformat())
        older = make_story(id="o", title="Older backlog", score=60,
                           first_seen_at=(datetime.now(CST) - timedelta(days=4)).isoformat())
        slots = select_slots([], self.cfg, set(), [recent, older])
        ids = [s["id"] for s in slots["general"]]
        self.assertEqual(ids[0], "r")
        self.assertLess(slots["general"][1]["_decayed"], slots["general"][0]["_decayed"])

    def test_empty_day_is_honest(self):
        """彻底没有内容时如实返回空，不硬凑。"""
        slots = select_slots([], self.cfg, set(), [])
        self.assertEqual(slots["general"], [])
        self.assertEqual(slots["mode"], "empty")

    def test_backlog_excludes_already_pushed(self):
        """历史补位也不能重复推送。"""
        backlog = [make_story(id="b1", title="Backlog", score=70,
                              first_seen_at=(datetime.now(CST) - timedelta(days=1)).isoformat())]
        slots = select_slots([], self.cfg, {"b1"}, backlog)
        self.assertEqual(slots["general"], [])

    def test_three_day_sequence_has_no_duplicates(self):
        """连续三天：所有推送内容互不重复。"""
        pushed: set[str] = set()
        seen_all: list[str] = []
        for day in range(3):
            fresh = [
                story_at(1, id=f"day{day}s{i}", score=68 - i)
                for i in range(4)
            ]
            slots = select_slots(fresh, self.cfg, pushed, [])
            ids = [s["id"] for s in slots["general"]]
            self.assertEqual(len(ids), len(set(ids)), f"第 {day + 1} 天内出现重复")
            self.assertEqual(set(ids) & pushed, set(), f"第 {day + 1} 天与之前重复")
            pushed |= set(ids)
            seen_all.extend(ids)

        self.assertEqual(len(seen_all), len(set(seen_all)), "三天累计出现重复推送")
        self.assertEqual(len(seen_all), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
