"""交叉印证、矛盾检测与中文社区卡的回归测试。

对应核查报告与设计：
- §6.3 claim 级交叉印证 / 矛盾检测
- §7.3 中文社区独立卡
- BUG-28 商品判定放宽
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.pipeline import (  # noqa: E402
    aggregate_claims,
    claim_tokens,
    cn_scarcity,
    cn_source_trust,
    confidence_text,
    is_product_item,
    score_cn_story,
    tie_break_key,
)
from test_pipeline import make_config, make_story  # noqa: E402


def _doc(doc_id, domain, tier, claims):
    return (
        {"id": doc_id, "source_id": f"src{doc_id}", "tier": tier,
         "url": f"https://{domain}/a", "publisher_domain": domain},
        {"claims": claims, "fallback": False},
    )


def _claim(text, ctype="statement", evidence="原文"):
    return {"text": text, "type": ctype, "evidence": evidence}


class TestClaimAggregation(unittest.TestCase):
    def test_same_claim_from_two_domains_is_promoted(self):
        pairs = [
            _doc(1, "warhammer-community.com", "L0",
                 [_claim("新 Codex 将于 12 月发售", "date")]),
            _doc(2, "goonhammer.com", "L1",
                 [_claim("新 Codex 将于 12 月发售", "date")]),
        ]
        claims, conflict = aggregate_claims(pairs)
        self.assertEqual(len(claims), 1)
        self.assertTrue(claims[0]["promoted"])
        self.assertEqual(claims[0]["support_count"], 2)
        self.assertFalse(conflict)

    def test_same_domain_duplicate_is_not_promoted(self):
        """同一域名的重复收录不算「多源印证」（BUG-03）。"""
        pairs = [
            _doc(1, "bilibili.com", "L2", [_claim("剑卫老兵模型展示")]),
            _doc(2, "bilibili.com", "L2", [_claim("剑卫老兵模型展示")]),
        ]
        claims, _ = aggregate_claims(pairs)
        self.assertFalse(claims[0]["promoted"])
        self.assertEqual(claims[0]["support_count"], 1)

    def test_l3_only_support_is_not_promoted(self):
        pairs = [
            _doc(1, "4chan.org", "L3", [_claim("新帝皇模型即将公布")]),
            _doc(2, "spikeybits.com", "L3", [_claim("新帝皇模型即将公布")]),
        ]
        claims, _ = aggregate_claims(pairs)
        self.assertFalse(claims[0]["promoted"], "L3 之间的互相支持不得升级")

    def test_conflicting_price_is_detected(self):
        pairs = [
            _doc(1, "warhammer-community.com", "L0", [_claim("定价 £35", "price")]),
            _doc(2, "goonhammer.com", "L1", [_claim("定价 £40", "price")]),
        ]
        claims, conflict = aggregate_claims(pairs)
        self.assertTrue(conflict, "同一主张出现不同价格应判为矛盾")
        self.assertEqual(claims[0]["contradict"], 1)

    def test_chinese_and_latin_claims_both_tokenised(self):
        self.assertTrue(claim_tokens("新 Codex 将于 12 月发售"))
        # 中文按二元字符组切分，不能整句当一个 token
        self.assertGreater(len(claim_tokens("新版本发售")), 1)

    def test_empty_pairs(self):
        claims, conflict = aggregate_claims([])
        self.assertEqual(claims, [])
        self.assertFalse(conflict)


class TestConfidenceText(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(confidence_text("L0", 1, "", False), "官方一手")
        self.assertEqual(confidence_text("L1", 2, "多源印证升级为 L1", False), "多源印证")
        self.assertEqual(confidence_text("L1", 2, "", False), "权威报道")
        self.assertEqual(confidence_text("L2", 1, "", False), "单一来源")
        self.assertEqual(confidence_text("L2", 3, "", False), "社区多源")
        self.assertEqual(confidence_text("L3", 1, "", False), "未证实")

    def test_conflict_wins(self):
        self.assertEqual(confidence_text("L0", 3, "", True), "主张存在矛盾")


class TestCnCard(unittest.TestCase):
    def test_cn_score_differs_from_general_score(self):
        cfg = make_config()
        story = make_story(
            id="c1", score=30, impact_scope="cn_community",
            title="战锤中文维基上线，国内玩家可查简体中文规则",
            summary="B站与贴吧同步更新，淘宝已有中文版预售",
        )
        scored = score_cn_story(story, cfg)
        self.assertGreater(scored["cn_score"], 30)
        self.assertIn("scarcity", json.loads(scored["cn_breakdown"]))

    def test_evergreen_content_is_penalised(self):
        cfg = make_config()
        base = dict(id="c2", score=40, impact_scope="cn_community",
                    summary="B站战锤频道发布，国内可看")
        normal = score_cn_story(make_story(**base, title="官方公布新版本规则更新"), cfg)
        paint = score_cn_story(make_story(**base, title="战锤涂装教程 第14期"), cfg)
        self.assertLess(paint["cn_score"], normal["cn_score"])
        self.assertLess(json.loads(paint["cn_breakdown"])["evergreen_penalty"], 1.0)

    def test_source_trust_ladder(self):
        self.assertEqual(cn_source_trust(["gcores"], "cn_community"), 4.0)
        self.assertEqual(cn_source_trust(["bilibili_40k"], "cn_community"), 3.0)
        self.assertEqual(cn_source_trust(["whatever"], "official"), 5.0)

    def test_scarcity_prefers_zh_only(self):
        zh_only = cn_scarcity(["bilibili_40k"], 1)
        mixed = cn_scarcity(["bilibili_40k", "gnews_40k"], 2)
        self.assertGreater(zh_only, mixed)


class TestTieBreak(unittest.TestCase):
    def test_same_score_orders_by_magnitude_then_authority(self):
        a = make_story(id="a", score=60, magnitude=4.0, authenticity="L1")
        b = make_story(id="b", score=60, magnitude=4.0, authenticity="L0")
        c = make_story(id="c", score=60, magnitude=2.0, authenticity="L0")
        order = [s["id"] for s in sorted([c, a, b], key=tie_break_key)]
        self.assertEqual(order[0], "b", "同分同量级时权威高者优先")
        self.assertEqual(order[-1], "c", "量级低者最后")

    def test_backlog_decayed_score_used(self):
        a = make_story(id="a", score=60)
        b = make_story(id="b", score=60)
        a["_decayed"] = 30.0
        b["_decayed"] = 55.0
        self.assertEqual([s["id"] for s in sorted([a, b], key=tie_break_key)], ["b", "a"])


class TestProductItem(unittest.TestCase):
    def test_preorder_news_counts_as_product(self):
        s = make_story(event_type="mini", actionability=4,
                       title="New Battleforce boxes now up for pre-order")
        self.assertTrue(is_product_item(s))

    def test_other_event_type_with_strong_signal_still_counts(self):
        s = make_story(event_type="other", actionability=4,
                       title="战锤40K 新模型开启预售")
        self.assertTrue(is_product_item(s), "强商品词应能突破 event_type=mini 的限定")

    def test_painting_tutorial_is_excluded(self):
        s = make_story(event_type="mini", actionability=4,
                       title="战锤40K 涂装教程：星际战士涂装分享")
        self.assertFalse(is_product_item(s))

    def test_low_actionability_excluded(self):
        s = make_story(event_type="mini", actionability=1, title="New kit revealed")
        self.assertFalse(is_product_item(s))


class TestWindowSemantics(unittest.TestCase):
    """放宽中文源采集窗口后，必须区分「新近发布」和「新近发现」。"""

    def _cn(self, sid, hours_old):
        """模拟：刚刚采集到（discovered=now），但原文是 hours_old 小时前发布的。"""
        from datetime import datetime, timedelta

        from infotwitter.db import CST

        now = datetime.now(CST).replace(microsecond=0)
        published = (now - timedelta(hours=hours_old)).isoformat()
        return make_story(
            id=sid, score=40, impact_scope="cn_community", magnitude=3.0,
            title="战锤40K 中文版新规则公布，国内同步预售",
            summary="B站与贴吧同步更新，淘宝已开订",
            first_seen_at=now.isoformat(), published_at=published,
        )

    def test_five_day_old_cn_story_can_fill_slot6_but_not_general(self):
        from infotwitter.pipeline import select_slots

        cfg = make_config(**{"windows.cn_slot_days": 7})
        stories = [self._cn("old5", 5 * 24)]
        slots = select_slots(stories, cfg, set(), [])
        self.assertEqual(len(slots["general"]), 0, "一周前的新闻不得进通识槽")
        self.assertIsNotNone(slots["cn"], "但可以进中文社区槽")
        self.assertGreaterEqual(slots["cn"]["age_days"], 4.9)

    def test_story_older_than_cn_window_is_excluded(self):
        from infotwitter.pipeline import select_slots

        cfg = make_config(**{"windows.cn_slot_days": 7})
        stories = [self._cn("old9", 9 * 24)]
        slots = select_slots(stories, cfg, set(), [])
        self.assertEqual(len(slots["general"]), 0)
        self.assertIsNone(slots["cn"])

    def test_fresh_cn_story_appears_in_both_pools(self):
        from infotwitter.pipeline import select_slots

        cfg = make_config(**{"windows.cn_slot_days": 7})
        stories = [self._cn("new", 3)]
        slots = select_slots(stories, cfg, set(), [])
        self.assertIsNotNone(slots["cn"])


if __name__ == "__main__":
    unittest.main()
