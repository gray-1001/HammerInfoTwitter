"""呈现层与推送通道的回归测试。

覆盖核查报告里的 P0/P1：
- BUG-01 推送链接路径
- BUG-02 推送标题不得取 HTML 首行
- BUG-16 HTML 版必须带 why_matters 与「未证实」徽章
- BUG-17 URL 不得造成属性注入 / 不得放行 javascript:
- BUG-18 摘要截断按通道预算决定，不再一律 60 字
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter.notify import PushPlusNotifier, WeComBotNotifier  # noqa: E402
from infotwitter.render import (  # noqa: E402
    build_push_html,
    build_push_text,
    build_push_title,
    safe_url,
)


def _story(**kw):
    base = {
        "id": "s1",
        "canonical_title": "New Ork codex arriving in December",
        "title_zh": "兽人圣典将于 12 月发售",
        "summary": "官方公布新兽人圣典，含 3 个新分队与 12 个新单位，12 月 6 日开订。" * 3,
        "why_matters": "兽人玩家需要重新规划军表。",
        "event_type": "rules",
        "authenticity": "L0",
        "importance_total": 72.5,
        "primary_url": "https://www.warhammer-community.com/a/b/",
    }
    base.update(kw)
    return base


def _slots(general=None, reason=None, cn=None, product_rows=None):
    items = general if general is not None else [_story(_slot_reason=reason or "qualified")]
    return {
        "mode": "normal",
        "general": items,
        "product": [],
        "product_rows": product_rows or [],
        "product_refs": [],
        "cn": cn,
    }


class TestPushTitle(unittest.TestCase):
    def test_title_is_plain_text_with_date(self):
        title = build_push_title(_slots(), "2026-11-17")
        self.assertIn("11-17", title)
        self.assertNotIn("<", title)

    def test_title_marks_sparse_day(self):
        slots = _slots(general=[_story(), _story(id="s2")])
        self.assertIn("仅2条", build_push_title(slots, "2026-11-17"))

    def test_title_marks_rumor_fill_mode(self):
        slots = _slots()
        slots["mode"] = "rumor_fill"
        self.assertIn("传闻补位", build_push_title(slots, "2026-11-17"))


class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self.content = b"{}"
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None):
        self.payload = payload or {"code": 200, "msg": "ok"}
        self.last_json = None

    def post(self, url, json=None):  # noqa: A002 - 与 httpx 签名一致
        self.last_json = json
        return _FakeResp(self.payload)


class TestPushPlusNotifier(unittest.TestCase):
    """BUG-02：标题必须来自调用方，不能从 HTML 首行截取。"""

    def test_uses_explicit_title_not_first_line(self):
        notifier = PushPlusNotifier("token-0123456789", template="html")
        fake = _FakeClient()
        notifier._client = fake
        html = build_push_html(_slots(), "2026-11-17", "http://h/2026-11-17.html")
        ok, _ = notifier.send("【战锤日报 11-17】", html)
        self.assertTrue(ok)
        self.assertEqual(fake.last_json["title"], "【战锤日报 11-17】")
        self.assertNotIn("<div", fake.last_json["title"])
        self.assertEqual(fake.last_json["template"], "html")

    def test_content_type_follows_template(self):
        self.assertEqual(PushPlusNotifier("t", template="txt").content_type, "text")
        self.assertEqual(PushPlusNotifier("t", template="html").content_type, "html")

    def test_reports_non_200_code_as_failure(self):
        notifier = PushPlusNotifier("token-0123456789", template="html")
        notifier._client = _FakeClient({"code": 903, "msg": "无效的用户令牌"})
        ok, msg = notifier.send("t", "body")
        self.assertFalse(ok)
        self.assertIn("903", msg)


class TestHtmlPush(unittest.TestCase):
    """BUG-16：HTML 版是当前唯一启用的通道，不允许丢字段。"""

    def test_contains_why_matters(self):
        html = build_push_html(_slots(), "2026-11-17", "http://h/x.html")
        self.assertIn("兽人玩家需要重新规划军表", html)
        self.assertIn("💡", html)

    def test_contains_unverified_badge_for_rumor_fill(self):
        html = build_push_html(
            _slots(general=[_story(_slot_reason="rumor")]), "2026-11-17", "http://h/x.html"
        )
        self.assertIn("未证实", html)

    def test_contains_history_badge_for_backlog(self):
        html = build_push_html(
            _slots(general=[_story(_slot_reason="backlog")]), "2026-11-17", "http://h/x.html"
        )
        self.assertIn("历史推荐", html)

    def test_event_type_is_localised(self):
        html = build_push_html(_slots(), "2026-11-17", "http://h/x.html")
        self.assertIn("规则", html)
        self.assertNotIn("· rules<", html)

    def test_cn_slot_carries_why_matters(self):
        cn = _story(id="c1", why_matters="中文玩家可关注引进进度。")
        html = build_push_html(_slots(cn=cn), "2026-11-17", "http://h/x.html")
        self.assertIn("中文玩家可关注引进进度", html)

    def test_report_link_has_no_reports_prefix(self):
        """BUG-01：服务以 report_dir 为根，链接不应带 /reports/。"""
        html = build_push_html(_slots(), "2026-11-17", "http://192.168.1.2:8080/2026-11-17.html")
        self.assertIn("http://192.168.1.2:8080/2026-11-17.html", html)
        self.assertNotIn("/reports/", html)


class TestUrlsAndEscaping(unittest.TestCase):
    """BUG-17：第三方可控的 URL 不得注入属性、不得放行 javascript:。"""

    def test_safe_url_allows_http_and_https(self):
        self.assertEqual(safe_url("https://a/b"), "https://a/b")
        self.assertEqual(safe_url("http://a/b"), "http://a/b")

    def test_safe_url_rejects_dangerous_schemes(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "  ", None):
            self.assertEqual(safe_url(bad), "")

    def test_url_attribute_injection_is_escaped(self):
        payload = 'https://x/" onmouseover="alert(1)'
        html = build_push_html(
            _slots(general=[_story(primary_url=payload)]), "2026-11-17", "http://h/x.html"
        )
        self.assertNotIn('onmouseover="alert(1)"', html)
        self.assertIn("&quot;", html)

    def test_javascript_url_not_rendered_as_link(self):
        html = build_push_html(
            _slots(general=[_story(primary_url="javascript:alert(1)")]),
            "2026-11-17",
            "http://h/x.html",
        )
        self.assertNotIn("javascript:alert(1)", html)

    def test_text_injection_is_escaped(self):
        html = build_push_html(
            _slots(general=[_story(title_zh="<script>alert(1)</script>")]),
            "2026-11-17",
            "http://h/x.html",
        )
        self.assertNotIn("<script>alert(1)</script>", html)


class TestSummaryBudget(unittest.TestCase):
    """BUG-18：摘要截断按通道字节预算决定。"""

    def _long_summary(self):
        return "官方公布新兽人圣典，含 3 个新分队与 12 个新单位，12 月 6 日开订。" * 3

    def test_large_budget_keeps_full_summary(self):
        story = _story(summary=self._long_summary())
        text = build_push_text(_slots(general=[story]), "2026-11-17", "http://x", max_bytes=20000)
        self.assertIn("12 个新单位", text)
        # 120 字上限：不应在 60 字处被截
        self.assertGreater(len(text), 120)

    def test_small_budget_still_respects_byte_limit(self):
        story = _story(summary=self._long_summary())
        text = build_push_text(_slots(general=[story]), "2026-11-17", "http://x", max_bytes=400)
        self.assertLessEqual(len(text.encode("utf-8")), 400)


class TestWeComNotifier(unittest.TestCase):
    """企微通道的标题应拼进正文首行。"""

    def test_title_prefixed_once(self):
        n = WeComBotNotifier("https://example.invalid/hook")
        fake = _FakeClient({"errcode": 0, "errmsg": "ok"})
        n._client = fake
        ok, _ = n.send("【战锤日报 11-17】", "① 标题")
        self.assertTrue(ok)
        self.assertTrue(fake.last_json["text"]["content"].startswith("【战锤日报 11-17】"))


if __name__ == "__main__":
    unittest.main()
