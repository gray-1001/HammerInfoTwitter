"""两级时间窗测试：48h 优先、48h–一周降级补位、超过一周剔除。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.db import CST  # noqa: E402
from infotwitter.pipeline import dedupe_same_title, select_slots  # noqa: E402
from test_pipeline import make_config, make_story  # noqa: E402


def story(hours_ago: float, **kw):
    t = (datetime.now(CST) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
    kw.setdefault("published_at", t)
    return make_story(first_seen_at=t, **kw)


class TestTwoTierWindow(unittest.TestCase):
    def setUp(self):
        # 采集一周、48h 内优先
        self.cfg = make_config(**{
            "windows.collect_hours": 168,
            "windows.push_hours": 48,
            "windows.backlog_days": 7,
            "thresholds.aged_min_score": 50,
        })

    def test_slots_scarce_fresh_wins(self):
        """槽位不够时，48h 内的条目优先占位；一周内的旧事件只能补空缺。"""
        fresh = [
            story(10, id="f1", title="Fresh custodians codex reveal", score=62, event_type="rules"),
            story(20, id="f2", title="Fresh battleforce preorder", score=61, event_type="mini"),
            story(30, id="f3", title="Fresh tournament rules pack", score=60, event_type="rules"),
        ]
        aged_high = [story(70, id="a1", title="Older chaos knights reveal", score=90, event_type="rules")]
        slots = select_slots(fresh + aged_high, self.cfg, set(), [])
        picked = {s["id"] for s in slots["general"]}
        self.assertTrue({"f1", "f2", "f3"} <= picked, "48h 内三条都该占位")
        self.assertIn("a1", picked, "剩一个槽由一周内的高分旧事件补")

    def test_fresh_only_when_enough(self):
        """48h 内已够 4 条时，一周内的高分旧事件不参与。"""
        fresh = [
            story(5, id=f"n{i}", title=t, score=64 - i, event_type="rules")
            for i, t in enumerate([
                "Adeptus Custodes codex preview", "Ork buggy new kit review",
                "Age of Sigmar campaign book announced", "Old World bretonnia return",
            ])
        ]
        slots = select_slots(fresh + [story(80, id="old90", title="Older huge reveal", score=95,
                                            event_type="rules")], self.cfg, set(), [])
        self.assertNotIn("old90", {s["id"] for s in slots["general"]})

    def test_aged_marked_with_reason(self):
        slots = select_slots(
            [story(72, id="old", title="Three day old news", score=66, event_type="rules")],
            self.cfg, set(), [],
        )
        self.assertEqual(len(slots["general"]), 1)
        self.assertEqual(slots["general"][0]["_slot_reason"], "aged")
        self.assertLess(slots["general"][0]["_decayed"], 66)

    def test_beyond_one_week_is_dropped(self):
        slots = select_slots(
            [story(200, id="too_old", title="Nine day old news", score=95, event_type="rules")],
            self.cfg, set(), [],
        )
        self.assertEqual(slots["general"], [])

    def test_aged_below_threshold_not_used(self):
        """一周内但分数太低的旧事件不补位（宁可少推）。"""
        slots = select_slots(
            [story(96, id="weak", title="Weak old news", score=40, event_type="rules")],
            self.cfg, set(), [],
        )
        self.assertEqual(slots["general"], [])

    def test_aged_respects_pushed_ledger(self):
        slots = select_slots(
            [story(72, id="pushed_old", title="Already pushed old", score=70, event_type="rules")],
            self.cfg, {"pushed_old"}, [],
        )
        self.assertEqual(slots["general"], [])

    def test_full_fresh_pool_ignores_aged(self):
        headlines = [
            "Adeptus Custodes codex preview", "Ork buggy new kit review",
            "Age of Sigmar campaign book announced", "Old World bretonnia return",
            "Kill team terrain expansion",
        ]
        stories = [
            story(10 + i, id=f"f{i}", title=headlines[i], score=70 - i, event_type="rules")
            for i in range(5)
        ] + [story(80, id="old", title="Ancient horrible rumour", score=90, event_type="rules")]
        slots = select_slots(stories, self.cfg, set(), [])
        ids = [s["id"] for s in slots["general"]]
        self.assertEqual(len(ids), 4)
        self.assertNotIn("old", ids, "48h 内容充足时不应动用一周内的旧事件")


class TestSameTitleDedupe(unittest.TestCase):
    """同一事件被聚成两条 story 时不能重复推送。"""

    def test_same_title_keeps_higher_score(self):
        a = make_story(id="a", title="周日预览：全新 Battleforce 套装", score=71.4)
        b = make_story(id="b", title="周日预览:全新 battleforce 套装!", score=67.0)
        kept = dedupe_same_title([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["id"], "a")

    def test_different_titles_both_kept(self):
        a = make_story(id="a", title="Battleforce 套装预览", score=70)
        b = make_story(id="b", title="Chaos Knights Battleforce", score=60)
        self.assertEqual(len(dedupe_same_title([a, b])), 2)

    def test_selection_does_not_repeat_title(self):
        cfg = make_config(**{"windows.collect_hours": 168, "windows.push_hours": 48})
        dup1 = story(5, id="n1", title="周日预览：全新 Battleforce 套装", score=71.4, event_type="mini")
        dup2 = story(120, id="o1", title="周日预览:全新 Battleforce 套装", score=67.0, event_type="mini")
        slots = select_slots([dup1, dup2], cfg, set(), [])
        titles = [s.get("canonical_title") for s in slots["general"]]
        self.assertEqual(len(titles), len(set(titles)), "同标题不应同时占两个槽")


if __name__ == "__main__":
    unittest.main(verbosity=2)
