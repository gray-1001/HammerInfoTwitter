"""首次推送后的针对性调整：标注发布日、槽 5 标题、槽 6 台账分。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.render import _age_note, build_push_text  # noqa: E402
from infotwitter.runner import _push_rows  # noqa: E402


class TestAgeNote(unittest.TestCase):
    """文章比日报早 2 天以上才标注，避免"今日日报"里藏着旧闻。"""

    def test_no_note_for_same_day(self):
        item = {"published_at": "2026-11-18T08:00:00+08:00"}
        self.assertEqual(_age_note(item, "2026-11-18"), "")

    def test_no_note_for_one_day(self):
        item = {"published_at": "2026-11-17T08:00:00+08:00"}
        self.assertEqual(_age_note(item, "2026-11-18"), "")

    def test_note_for_six_days(self):
        item = {"published_at": "2026-11-12T00:00:00+08:00"}
        self.assertEqual(_age_note(item, "2026-11-18"), " · 发布于 11-12")

    def test_missing_date_is_silent(self):
        self.assertEqual(_age_note({}, "2026-11-18"), "")
        self.assertEqual(_age_note({"published_at": "坏数据"}, "2026-11-18"), "")

    def test_bad_digest_date_is_silent(self):
        item = {"published_at": "2026-11-12T00:00:00+08:00"}
        self.assertEqual(_age_note(item, ""), "")


class TestPushTextSlotTitles(unittest.TestCase):
    """槽 5 的标题在文本、HTML、台账三处必须一致（P2-5）。"""

    def test_product_list_title(self):
        slots = {"general": [], "product": [{"title_zh": "混沌骑士战团包"}],
                 "product_rows": [], "cn": None}
        text = build_push_text(slots, "2026-11-18", "http://x", max_bytes=20000)
        self.assertIn("模型价格速览", text)
        self.assertNotIn("本周模型快讯", text)


class TestPushRowsLedger(unittest.TestCase):
    """台账记的必须是读者看到的东西。"""

    def _db(self):
        import tempfile

        from infotwitter.db import Database

        return Database(Path(tempfile.mkdtemp()) / "t.db")

    def test_slot5_title_matches_body(self):
        db = self._db()
        slots = {"general": [], "product_rows": [{"id": "39-13"}], "product": []}
        rows, _ = _push_rows(db, "2026-11-18", slots, "<p>", "pushplus")
        slot5 = [r for r in rows if r["slot"] == 5][0]
        self.assertEqual(slot5["title"], "模型价格速览")
        db.close()

    def test_slot6_records_cn_score(self):
        db = self._db()
        cn = {"id": "c1", "title_zh": "广州战锤粉", "importance_total": 25.2, "cn_score": 82.5}
        rows, _ = _push_rows(db, "2026-11-18", {"general": [], "cn": cn}, "<p>", "pushplus")
        slot6 = [r for r in rows if r["slot"] == 6][0]
        self.assertEqual(slot6["importance"], 82.5, "台账要记读者看到的 82.5，不是通识分 25.2")
        db.close()

    def test_slot6_falls_back_when_no_cn_score(self):
        db = self._db()
        cn = {"id": "c2", "title_zh": "x", "importance_total": 30.0}
        rows, _ = _push_rows(db, "2026-11-18", {"general": [], "cn": cn}, "<p>", "pushplus")
        self.assertEqual([r for r in rows if r["slot"] == 6][0]["importance"], 30.0)
        db.close()


if __name__ == "__main__":
    unittest.main()

class TestBuzzDownweight(unittest.TestCase):
    """N-2：转载型新闻不能靠"被转了几次"拿高分。"""

    def _doc(self, domain, tier):
        return {"id": 1, "publisher_domain": domain, "tier": tier, "url": "https://x"}

    def test_subdomains_merge(self):
        from infotwitter.pipeline import registrable_domain

        self.assertEqual(registrable_domain("blogs.nvidia.com"), "nvidia.com")
        self.assertEqual(registrable_domain("www.belloflostsouls.net"), "belloflostsouls.net")
        self.assertEqual(registrable_domain("news.goonhammer.com"), "goonhammer.com")
        self.assertEqual(registrable_domain("example.co.uk"), "example.co.uk")

    def test_same_vendor_not_two_sources(self):
        from infotwitter.pipeline import independent_sources

        docs = [self._doc("nvidia.com", "L2"), self._doc("blogs.nvidia.com", "L2")]
        self.assertEqual(independent_sources(docs), {"nvidia.com"})

    def test_reprint_weighs_less_than_original(self):
        from infotwitter.pipeline import independent_weight

        reprinted = [self._doc("nvidia.com", "L2"), self._doc("tweaktown.com", "L2")]
        original = [self._doc("belloflostsouls.net", "L1"), self._doc("goonhammer.com", "L1")]
        self.assertLess(independent_weight(reprinted), independent_weight(original),
                        "两家转载媒体不该等于两家原创报道")

    def test_buzz_uses_weight(self):
        from infotwitter.pipeline import buzz_score

        self.assertAlmostEqual(buzz_score(3, 1.2), 1.18, places=2)
        self.assertAlmostEqual(buzz_score(3), 2.8, places=2, msg="不传权重时保持旧口径")

    def test_driver_story_no_longer_top(self):
        """实证：驱动新闻三家（两家属同厂商）加权后低于真新闻。"""
        from infotwitter.pipeline import independent_weight

        driver = [self._doc("nvidia.com", "L2"), self._doc("blogs.nvidia.com", "L2"),
                  self._doc("tweaktown.com", "L2")]
        real = [self._doc("belloflostsouls.net", "L1"), self._doc("belloflostsouls.net", "L1")]
        self.assertLess(independent_weight(driver), 2.0)
        self.assertEqual(independent_weight(real), 1.0)


if __name__ == "__main__":
    unittest.main()

