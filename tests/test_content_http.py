"""M2 回归测试：HTTP 优先抓取、URL 跳过规则、浏览器兜底决策。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter import content  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    """按 URL 返回预设响应，同时记录调用。"""

    def __init__(self, mapping: dict[str, FakeResponse]):
        self.mapping = mapping
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        resp = self.mapping.get(url)
        if isinstance(resp, Exception):
            raise resp
        return resp or FakeResponse(404, "")

    def close(self):
        pass


ARTICLE = "<html><body><p>" + ("这是一篇完整的战锤文章内容。" * 60) + "</p></body></html>"
SHORT = "<html><body><p>太短</p></body></html>"
BLOCKED = "<html><body><p>Just a moment... security verification</p></body></html>"


class TestUselessUrl(unittest.TestCase):
    def test_google_news_skipped(self):
        self.assertTrue(content.is_useless_url("https://news.google.com/rss/articles/CBMi123"))

    def test_youtube_skipped(self):
        self.assertTrue(content.is_useless_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(content.is_useless_url("https://youtu.be/abc"))

    def test_reddit_skipped(self):
        self.assertTrue(content.is_useless_url("https://www.reddit.com/r/Warhammer/abc/"))

    def test_normal_article_not_skipped(self):
        self.assertFalse(content.is_useless_url("https://www.warhammer-community.com/en-gb/x/"))
        self.assertFalse(content.is_useless_url("https://www.bilibili.com/video/BV1xx"))


class TestFetchHttpOne(unittest.TestCase):
    def test_ok_returns_text(self):
        client = FakeClient({"https://a/x": FakeResponse(200, ARTICLE)})
        text, why = content.fetch_http_one(client, "https://a/x")
        self.assertGreaterEqual(len(text), content.MIN_CHARS)
        self.assertEqual(why, "")

    def test_403_asks_for_browser(self):
        client = FakeClient({"https://a/x": FakeResponse(403, "<html>forbidden</html>")})
        text, why = content.fetch_http_one(client, "https://a/x")
        self.assertEqual((text, why), ("", "block"))

    def test_429_asks_for_browser(self):
        client = FakeClient({"https://a/x": FakeResponse(429, "")})
        _, why = content.fetch_http_one(client, "https://a/x")
        self.assertEqual(why, "block")

    def test_short_text_asks_for_browser(self):
        client = FakeClient({"https://a/x": FakeResponse(200, SHORT)})
        _, why = content.fetch_http_one(client, "https://a/x")
        self.assertEqual(why, "short")

    def test_challenge_page_asks_for_browser(self):
        client = FakeClient({"https://a/x": FakeResponse(200, BLOCKED)})
        _, why = content.fetch_http_one(client, "https://a/x")
        self.assertEqual(why, "block")

    def test_useless_url_never_requests(self):
        client = FakeClient({})
        text, why = content.fetch_http_one(client, "https://news.google.com/rss/articles/x")
        self.assertEqual((text, why), ("", "skip"))
        self.assertEqual(client.calls, [], "跳转页不应发起请求")

    def test_network_error_retried(self):
        client = FakeClient({"https://a/x": RuntimeError("boom")})
        _, why = content.fetch_http_one(client, "https://a/x", retries=2)
        self.assertEqual(why, "error")
        self.assertEqual(len(client.calls), 2, "应重试")


class TestFetchFulltextHttpFirst(unittest.TestCase):
    def _cfg(self, **over):
        class Cfg:
            proxy = None
            user_agent = "ua"

            def get(self, key, default=None):
                base = {
                    "content.http_first": True,
                    "content.http_retries": 1,
                    "content.browser_fallback": False,
                    "content.deadline_sec": 0,
                }
                base.update(over)
                return base.get(key, default)
        return Cfg()

    def test_http_only_path(self):
        client = FakeClient({
            "https://a/1": FakeResponse(200, ARTICLE),
            "https://a/2": FakeResponse(403, ""),
        })
        items = [{"url": "https://a/1"}, {"url": "https://a/2"}]
        with mock.patch.object(content, "_http_client", return_value=client):
            got = content.fetch_fulltext(items, None, "ua", max_items=10, cfg=self._cfg())
        self.assertIn("https://a/1", got, "HTTP 成功的应进结果")
        self.assertNotIn("https://a/2", got, "403 且禁用浏览器兜底时不应有结果")

    def test_browser_fallback_called_only_for_leftovers(self):
        client = FakeClient({
            "https://a/1": FakeResponse(200, ARTICLE),
            "https://a/2": FakeResponse(403, ""),
        })
        items = [{"url": "https://a/1"}, {"url": "https://a/2"}]
        called: list[str] = []

        def fake_browser(page, url, *a, **kw):
            called.append(url)
            return "浏览器拿到的正文" * 40

        with mock.patch.object(content, "_http_client", return_value=client), \
             mock.patch.object(content, "_fetch_one", side_effect=fake_browser), \
             mock.patch("playwright.sync_api.sync_playwright") as pw:
            pw.return_value.__enter__.return_value.chromium.launch.return_value \
                .new_context.return_value.new_page.return_value = object()
            got = content.fetch_fulltext(items, None, "ua", max_items=10,
                                         cfg=self._cfg(**{"content.browser_fallback": True}))
        self.assertEqual(called, ["https://a/2"], "只把 HTTP 失败的交给浏览器")
        self.assertIn("https://a/1", got)
        self.assertIn("https://a/2", got)

    def test_youtube_skips_browser(self):
        """视频站抓了也拿不到正文，连浏览器都不该启动。"""
        client = FakeClient({})
        items = [{"url": "https://www.youtube.com/watch?v=abc"}]
        with mock.patch.object(content, "_http_client", return_value=client), \
             mock.patch("playwright.sync_api.sync_playwright") as pw:
            got = content.fetch_fulltext(items, None, "ua", max_items=10,
                                         cfg=self._cfg(**{"content.browser_fallback": True}))
        self.assertEqual(got, {})
        pw.assert_not_called()

    def test_google_news_goes_to_browser_for_resolution(self):
        """Google News 是 JS 壳，HTTP 抓不到，但浏览器能还原到真实文章页。"""
        client = FakeClient({})
        items = [{"url": "https://news.google.com/rss/articles/x"}]
        seen: list[str] = []

        def fake_resolve(page, url, wait_ms, **kw):
            seen.append(url)
            bucket = kw.get("resolved")
            if bucket is not None:
                bucket[url] = "https://www.example.com/real-article"
            return "还原后的正文内容。" * 60

        with mock.patch.object(content, "_http_client", return_value=client), \
             mock.patch.object(content, "_resolve_and_fetch", side_effect=fake_resolve), \
             mock.patch("playwright.sync_api.sync_playwright") as pw:
            pw.return_value.__enter__.return_value.chromium.launch.return_value \
                .new_context.return_value.new_page.return_value = object()
            got = content.fetch_fulltext(items, None, "ua", max_items=10,
                                         cfg=self._cfg(**{"content.browser_fallback": True}))
        self.assertEqual(seen, ["https://news.google.com/rss/articles/x"])
        self.assertTrue(got, "还原成功时应有正文")
        self.assertEqual(client.calls, [], "Google News 不该走 HTTP")


class TestHostLearning(unittest.TestCase):
    """同一域名连续失败后，本轮不再浪费时间走 HTTP。"""

    def _cfg(self):
        class Cfg:
            proxy = None
            user_agent = "ua"

            def get(self, key, default=None):
                return {
                    "content.http_first": True,
                    "content.http_retries": 1,
                    "content.browser_fallback": True,
                    "content.http_dead_threshold": 2,
                    "content.deadline_sec": 0,
                }.get(key, default)
        return Cfg()

    def test_blocked_host_stops_after_threshold(self):
        # 同一域名两条都 403
        client = FakeClient({
            "https://b/1": FakeResponse(403, ""),
            "https://b/2": FakeResponse(403, ""),
            "https://b/3": FakeResponse(403, ""),
        })
        items = [{"url": f"https://b/{i}"} for i in (1, 2, 3)]
        seen: list[str] = []

        def fake_browser(page, url, *a, **kw):
            seen.append(url)
            return "浏览器正文" * 100

        with mock.patch.object(content, "_http_client", return_value=client), \
             mock.patch.object(content, "_fetch_one", side_effect=fake_browser), \
             mock.patch("playwright.sync_api.sync_playwright") as pw:
            pw.return_value.__enter__.return_value.chromium.launch.return_value \
                .new_context.return_value.new_page.return_value = object()
            got = content.fetch_fulltext(items, None, "ua", max_items=10, cfg=self._cfg())

        # 前两条走 HTTP 失败，第三条应直接进浏览器队列，不再请求
        self.assertEqual(len(client.calls), 2, "达到阈值后不应再发 HTTP 请求")
        self.assertEqual(len(seen), 3, "三条最终都要经浏览器兜底")
        self.assertEqual(len(got), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
