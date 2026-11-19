"""修复项回归测试：可信度、时间窗、台账、价格、反馈、采集健壮性。

对应核查报告编号：
BUG-03 多源计数 / BUG-04 claim 证据 / BUG-05 首次出现 / BUG-07 status 保护
BUG-09 feed 有效性 / BUG-19 预期价 / BUG-20 版本守卫 / BUG-22 幂等
BUG-42 反馈去重 / BUG-43 bilibili 哈希与容错
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import feedparser  # noqa: E402

from infotwitter.adapters import is_valid_feed  # noqa: E402
from infotwitter.adapters.bilibili import _hash as bili_hash, _safe_int  # noqa: E402
from infotwitter.db import Database  # noqa: E402
from infotwitter.llm import evidence_supported  # noqa: E402
from infotwitter.pricing import edition_signature, expected_price  # noqa: E402
from infotwitter.tokens import feedback_token, verify_feedback_token  # noqa: E402


class TestFeedValidation(unittest.TestCase):
    """BUG-09：反爬页以 200 返回时不能被当成「窗口内无新内容」。"""

    def test_html_challenge_page_is_invalid(self):
        parsed = feedparser.parse(
            b"<html><head><title>Just a moment...</title></head><body>x</body></html>"
        )
        self.assertFalse(is_valid_feed(parsed))

    def test_real_rss_is_valid(self):
        parsed = feedparser.parse(
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
        )
        self.assertTrue(is_valid_feed(parsed))

    def test_empty_but_valid_feed_is_valid(self):
        """合法 feed 但窗口内 0 条 —— 必须与「解析失败」区分开。"""
        parsed = feedparser.parse(
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
        )
        self.assertEqual(len(parsed.entries), 0)
        self.assertTrue(is_valid_feed(parsed))


class TestEvidenceVerification(unittest.TestCase):
    """BUG-04：claim 的 evidence 必须是原文子串。"""

    HAY = "New Warhammer The Old World: 2027 Roadmap, Release Schedule Reveals"

    def test_exact_fragment_passes(self):
        self.assertTrue(evidence_supported("The Old World", self.HAY))

    def test_whitespace_and_case_insensitive(self):
        self.assertTrue(evidence_supported("the old   world", self.HAY))

    def test_fabricated_fragment_is_rejected(self):
        self.assertFalse(evidence_supported("Warhammer 40k 2026 路线图", self.HAY))

    def test_empty_evidence_is_rejected(self):
        self.assertFalse(evidence_supported("", self.HAY))


class TestBilibiliHelpers(unittest.TestCase):
    """BUG-43：哈希正则写错导致空白归一失效；play 字段可能非数字。"""

    def test_whitespace_is_normalised(self):
        a = bili_hash("战锤  40K   模型", "https://b/v1")
        b = bili_hash("战锤 40K 模型", "https://b/v1")
        self.assertEqual(a, b)

    def test_safe_int_handles_play_formats(self):
        self.assertEqual(_safe_int("12"), 12)
        self.assertEqual(_safe_int("1.2万"), 12000)
        self.assertEqual(_safe_int("--"), 0)
        self.assertEqual(_safe_int(None), 0)
        self.assertEqual(_safe_int(True), 0)


class TestDatabaseBehaviour(unittest.TestCase):
    """BUG-07 / BUG-22 / BUG-42：台账与反馈的写路径。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.tmp)

    def tearDown(self):
        self.db.close()

    def _story(self, **kw):
        base = {
            "id": "st1",
            "canonical_title": "t",
            "first_seen_at": "2026-11-17T10:00:00+08:00",
            "status": "scored",
        }
        base.update(kw)
        return base

    def test_status_is_not_reset_by_reinsert(self):
        self.db.insert_story(self._story())
        self.db.update_story_fields("st1", status="pushed")
        self.db.insert_story(self._story(canonical_title="t2"))
        row = self.db.conn.execute("SELECT status, canonical_title FROM stories WHERE id='st1'").fetchone()
        self.assertEqual(row["status"], "pushed", "重跑不应把已推送状态重置")
        self.assertEqual(row["canonical_title"], "t2", "其余字段应正常更新")

    def test_first_seen_is_sticky(self):
        self.db.insert_story(self._story())
        self.db.insert_story(self._story(first_seen_at="2026-11-18T10:00:00+08:00"))
        row = self.db.conn.execute("SELECT first_seen_at FROM stories WHERE id='st1'").fetchone()
        self.assertEqual(row["first_seen_at"], "2026-11-17T10:00:00+08:00")

    def test_pushed_today_sees_any_slot(self):
        self.assertFalse(self.db.pushed_today("2026-11-17"))
        self.db.record_push({
            "push_date": "2026-11-17", "slot": 5, "channel": "pushplus", "delivery_ok": 1,
        })
        self.assertTrue(self.db.pushed_today("2026-11-17"), "只写了槽 5 也算今日已推送")

    def test_failed_push_does_not_block_retry(self):
        self.db.record_push({
            "push_date": "2026-11-17", "slot": 1, "channel": "pushplus", "delivery_ok": 0,
        })
        self.assertFalse(self.db.pushed_today("2026-11-17"))

    def test_replace_claims_is_idempotent(self):
        self.db.insert_story(self._story())
        claims = [{"text": "定价 £35", "type": "price", "evidence": "£35", "raw_doc_id": 1}]
        self.assertEqual(self.db.replace_claims("st1", claims), 1)
        self.assertEqual(self.db.replace_claims("st1", claims), 1)
        self.assertEqual(self.db.claim_count(), 1, "重复写应覆盖而不是堆积")

    def test_feedback_dedup_same_day(self):
        self.assertTrue(self.db.claim_feedback_once("st1", "useful", "2026-11-17"))
        self.assertFalse(self.db.claim_feedback_once("st1", "useful", "2026-11-17"))
        self.assertTrue(self.db.claim_feedback_once("st1", "useful", "2026-11-18"))


class TestPricingGuards(unittest.TestCase):
    """BUG-19 / BUG-20：预期价模型可用，版本守卫能拦住错配。"""

    class _Cfg:
        def get(self, dotted, default=None):
            return {
                "pricing.rates": {"GBP_CNY": 9.20},
                "pricing.freight_rate": 0.08,
                "pricing.duty_rate": 0.09,
                "pricing.channel_markup": 1.12,
                "pricing.spread": 0.10,
            }.get(dotted, default)

    def test_expected_price_range_is_ordered(self):
        m = expected_price(109.0, "GBP", self._Cfg())
        self.assertIsNotNone(m)
        self.assertLess(m["low"], m["mid"])
        self.assertLess(m["mid"], m["high"])
        self.assertIn("基准价", m["breakdown"])

    def test_expected_price_needs_rate(self):
        self.assertIsNone(expected_price(0, "GBP", self._Cfg()))
        self.assertIsNone(expected_price(109.0, "GBP", None))

    def test_edition_signature_distinguishes_language(self):
        self.assertNotEqual(
            edition_signature("WARHAMMER 40K: CORE RULES (CHINESE)"),
            edition_signature("WARHAMMER 40K: CORE RULES 11TH EDITION (ENG)"),
        )

    def test_edition_signature_matches_same_edition(self):
        self.assertEqual(
            edition_signature("ARMAGEDDON BATTALION: DEATHWATCH"),
            edition_signature("ARMAGEDDON BATTALION: DEATHWATCH"),
        )


class TestFeedbackToken(unittest.TestCase):
    """BUG-25：反馈接口需要按天轮换的令牌。"""

    def test_token_roundtrip(self):
        tok = feedback_token("2026-11-17")
        self.assertTrue(verify_feedback_token("2026-11-17", tok))

    def test_token_is_day_scoped(self):
        tok = feedback_token("2026-11-17")
        self.assertFalse(verify_feedback_token("2026-11-18", tok))

    def test_empty_token_rejected(self):
        self.assertFalse(verify_feedback_token("2026-11-17", ""))


if __name__ == "__main__":
    unittest.main()
