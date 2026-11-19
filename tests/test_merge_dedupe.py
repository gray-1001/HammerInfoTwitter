"""同标题合并（N-02/N-07/N-16）、二级补位展示下限（N-05）、展示分口径（N-03）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.db import CST  # noqa: E402
from infotwitter.pipeline import (  # noqa: E402
    _title_key,
    dedupe_same_title,
    merge_same_title,
    select_slots,
)
from infotwitter.render import build_report  # noqa: E402
from test_pipeline import make_config, make_story  # noqa: E402


def story(hours_ago: float, **kw):
    t = (datetime.now(CST) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
    kw.setdefault("published_at", t)
    kw.setdefault("first_seen_at", t)
    return make_story(**kw)


class TestMergeSameTitle(unittest.TestCase):
    """N-02：合并必须发生在聚类之后、写库之前。"""

    TITLE = "Sunday Preview - New Battleforce Boxes Muster for Action"

    def test_merges_two_stories_with_same_title(self):
        a = story(6, id="a", title=self.TITLE, title_zh="周日预览：套装",
                  score=71.4, member_ids=[1, 2])
        b = story(117, id="b", title=self.TITLE, title_zh="周日预览：套装",
                  score=67.0, member_ids=[3])
        kept, merged = merge_same_title([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["id"], "b", "保留 first_seen 更早的一条（事件身份更久）")
        self.assertEqual(merged, ["a"])
        self.assertEqual(sorted(kept[0]["member_ids"]), [1, 2, 3], "成员文档要并集")

    def test_merged_story_claims_are_unioned_and_deduped(self):
        a = story(6, id="a", title=self.TITLE, score=70, member_ids=[1],
                  claims=[{"text": "X 发售", "raw_doc_id": 1}])
        b = story(100, id="b", title=self.TITLE, score=60, member_ids=[2],
                  claims=[{"text": "X 发售", "raw_doc_id": 2},
                          {"text": "Y 价格 100", "raw_doc_id": 2}])
        kept, _ = merge_same_title([a, b])
        texts = [c["text"] for c in kept[0]["claims"]]
        self.assertEqual(texts.count("X 发售"), 1, "重复 claim 只留一条")
        self.assertIn("Y 价格 100", texts)

    def test_different_titles_not_merged(self):
        a = story(6, id="a", title="Adeptus Custodes codex", score=70, member_ids=[1])
        b = story(6, id="b", title="Ork buggy review", score=70, member_ids=[2])
        kept, merged = merge_same_title([a, b])
        self.assertEqual(len(kept), 2)
        self.assertEqual(merged, [])

    def test_single_story_returned_as_is(self):
        a = story(6, id="a", title="Only one", score=70, member_ids=[1])
        kept, merged = merge_same_title([a])
        self.assertEqual(len(kept), 1)
        self.assertEqual(merged, [])


class TestTitleKeyUsesCanonical(unittest.TestCase):
    """N-07：去重键只看原文标题，不看 LLM 译名。"""

    def test_same_title_different_translation_is_same_key(self):
        a = {"canonical_title": "Sunday Preview - Battleforce", "title_zh": "周日预览：战团包"}
        b = {"canonical_title": "Sunday Preview - Battleforce", "title_zh": "星期日预览：战团包"}
        self.assertEqual(_title_key(a), _title_key(b))

    def test_different_titles_same_translation_are_different_keys(self):
        a = {"canonical_title": "Codex A", "title_zh": "战锤新闻"}
        b = {"canonical_title": "Codex B", "title_zh": "战锤新闻"}
        self.assertNotEqual(_title_key(a), _title_key(b), "不能因为译名相同就合并")

    def test_empty_key_story_is_not_dropped(self):
        """N-16：标题归一化后为空（纯符号）时不能静默丢弃。"""
        weird = {"id": "w1", "canonical_title": "!!!", "title_zh": "!!!", "importance_total": 50}
        normal = {"id": "n1", "canonical_title": "Real news", "importance_total": 60}
        kept = dedupe_same_title([weird, normal])
        self.assertEqual(len(kept), 2)


class TestAgedShownFloor(unittest.TestCase):
    """N-05：原始分过线但衰减后太低的事件不该推给读者。"""

    CFG = make_config(**{
        "windows.collect_hours": 168,
        "windows.push_hours": 48,
        "thresholds.aged_min_score": 50,
        "thresholds.aged_min_shown": 40,
    })

    def test_low_decayed_score_is_blocked(self):
        # 原始 55 分、6.6 天前 → 衰减约 31.8，低于展示下限
        s = story(158, id="weak_old", title="Six day old rumour",
                  score=55, event_type="rules")
        slots = select_slots([s], self.CFG, set(), [])
        self.assertEqual(slots["general"], [], "31 分级内容不应进推送")

    def test_reasonable_decayed_score_still_fills(self):
        # 原始 70 分、5 天前 → 衰减约 46，高于下限
        s = story(120, id="ok_old", title="Five day old reveal",
                  score=70, event_type="rules")
        slots = select_slots([s], self.CFG, set(), [])
        self.assertEqual(len(slots["general"]), 1)
        self.assertGreaterEqual(slots["general"][0]["_decayed"], 40)


class TestShownScoreConsistency(unittest.TestCase):
    """N-03：报告页显示的分数必须和推送一致（二级补位显示衰减分）。"""

    def test_report_uses_decayed_score(self):
        item = story(120, id="aged1", title="Older reveal",
                     score=70, event_type="rules")
        item.update({"_slot_reason": "aged", "_decayed": 46.1,
                     "score_breakdown": json.dumps({"raw": 70.0, "total": 70.0})})
        slots = {"mode": "aged_fill", "general": [item], "product": [], "product_rows": [],
                 "cn": None, "product_refs": [],
                 "stats": {"fresh": 0, "aged_pool": 1, "qualified": 0, "rumor_pool": 0,
                           "cn_pool": 0, "backlog_pool": 0, "undated_skipped": 0}}
        with tempfile.TemporaryDirectory() as td:
            path = build_report(slots, "2026-11-17", Path(td), {}, {}, [], write_latest=False)
            html = Path(path).read_text(encoding="utf-8")
        self.assertIn("46.1 分", html, "报告页应显示衰减分")
        self.assertNotIn(">70.0 分<", html, "不能显示未衰减的原始分")
        self.assertIn("时效衰减", html, "明细里要说明为什么和原始分不同")


if __name__ == "__main__":
    unittest.main()
