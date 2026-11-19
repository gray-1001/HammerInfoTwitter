"""第 4 批（采集层）回归测试：C-04/C-06/C-07/C-08/C-09/C-10/C-12。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter import content  # noqa: E402
from infotwitter.adapters import bilibili  # noqa: E402
from infotwitter.db import CST, Database  # noqa: E402
from infotwitter.httpclient import Fetcher, _retry_after_seconds  # noqa: E402


class TestRetryAfter(unittest.TestCase):
    """C-10：Retry-After 既可能是秒数，也可能是 HTTP-date。"""

    def test_seconds(self):
        self.assertEqual(_retry_after_seconds("5"), 5.0)
        self.assertEqual(_retry_after_seconds("0"), 0.0)

    def test_http_date(self):
        future = datetime.now().astimezone() + timedelta(seconds=60)
        from email.utils import format_datetime

        value = _retry_after_seconds(format_datetime(future))
        self.assertIsNotNone(value)
        self.assertLessEqual(value, 65)

    def test_invalid(self):
        self.assertIsNone(_retry_after_seconds("soon"))
        self.assertIsNone(_retry_after_seconds(None))
        self.assertIsNone(_retry_after_seconds(""))


class TestSocksFallback(unittest.TestCase):
    """C-12：SOCKS 代理缺依赖时降级直连，而不是让整轮采集崩掉。"""

    def test_import_error_falls_back(self):
        with mock.patch("httpx.Client", side_effect=ImportError("socksio missing")):
            # 第一次构造抛 ImportError，降级后第二次必须成功
            real_client = mock.Mock()
            with mock.patch("httpx.Client", side_effect=[ImportError("x"), real_client]):
                f = Fetcher(proxy="socks5://127.0.0.1:1080", timeout=10)
        self.assertIsNone(f.proxy, "降级后应清掉不可用的代理")
        self.assertIs(f._client, real_client)

    def test_no_proxy_untouched(self):
        f = Fetcher(proxy=None, timeout=10)
        self.assertIsNone(f.proxy)
        f.close()


class TestLooksBlocked(unittest.TestCase):
    """C-09：拦截标记落在正文深处也要能识别。"""

    def test_marker_deep_in_text(self):
        text = ("这是一篇看起来很正常的文章正文。" * 100) + " subscribe to read the rest"
        self.assertGreater(len(text), 600)
        self.assertTrue(content.looks_blocked(text), "第 600 字之后的标记也要扫到")

    def test_clean_text(self):
        self.assertFalse(content.looks_blocked("这是一篇正常文章。" * 50))


class TestCacheTTL(unittest.TestCase):
    """C-07：过期缓存读取时就该失效，而不是等 prune。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "c.db")

    def tearDown(self):
        self.db.close()

    def _put(self, url, days_ago):
        ts = (datetime.now(CST) - timedelta(days=days_ago)).isoformat()
        self.db.conn.execute(
            "INSERT OR REPLACE INTO content_cache (url_hash, url, text, char_count, fetched_at)"
            " VALUES (?,?,?,?,?)",
            (content._url_hash(url), url, "正文", 2, ts),
        )
        self.db.conn.commit()

    def test_fresh_cache_hit(self):
        self._put("https://x/a", 1)
        self.assertEqual(content.cache_get(self.db.conn, "https://x/a", max_age_days=14), "正文")

    def test_expired_cache_miss(self):
        self._put("https://x/b", 30)
        self.assertIsNone(content.cache_get(self.db.conn, "https://x/b", max_age_days=14))

    def test_no_ttl_returns_anything(self):
        self._put("https://x/c", 30)
        self.assertEqual(content.cache_get(self.db.conn, "https://x/c"), "正文")


class TestMaxItemsCap(unittest.TestCase):
    """C-08：max_items 不能等于"全部"。"""

    def test_cap_is_passed_through(self):
        db = Database(Path(tempfile.mkdtemp()) / "d.db")

        class Cfg:
            sources = [{"id": "s1", "deep_fetch": True}]
            proxy = None
            user_agent = "ua"

            def get(self, key, default=None):
                return {
                    "content.enabled": True,
                    "content.max_items": 3,
                    "content.max_docs": 0,
                }.get(key, default)

        docs = [{"id": i, "source_id": "s1", "url": f"https://x/{i}", "body": ""} for i in range(10)]
        captured = {}

        def fake_fetch(items, **kw):
            captured["max_items"] = kw.get("max_items")
            return {}

        with mock.patch.object(content, "fetch_fulltext", side_effect=fake_fetch):
            content.enrich_docs(Cfg(), db, docs)
        self.assertEqual(captured["max_items"], 3, "上限必须真的生效")
        db.close()


class TestBilibiliCookie(unittest.TestCase):
    """C-06：-500 / 网络异常不能把可用 cookie 删掉。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "cookies_bili.json"
        self.cache.write_text(json.dumps({"ts": int(time.time()), "cookies": {"SESSDATA": "x"}}),
                              encoding="utf-8")
        self.patch = mock.patch.object(bilibili, "CACHE_PATH", self.cache)

    def _run(self, payload):
        from infotwitter.adapters.bilibili import BilibiliAdapter

        adapter = BilibiliAdapter(None, "ua", None)
        resp = mock.Mock()
        resp.json.return_value = payload
        client = mock.Mock()
        client.__enter__ = mock.Mock(return_value=client)
        client.__exit__ = mock.Mock(return_value=False)
        client.get.return_value = resp
        with self.patch, mock.patch("httpx.Client", return_value=client):
            adapter.fetch({"id": "bili_wh", "keyword": "战锤", "lang": "zh"}, 
                          datetime.now(CST) - timedelta(days=7))
        return self.cache.exists()

    def test_server_error_keeps_cookie(self):
        self.assertTrue(self._run({"code": -500, "message": "server error"}),
                        "服务端错误不该删 cookie")

    def test_auth_error_clears_cookie(self):
        self.assertFalse(self._run({"code": -412, "message": "risk control"}),
                         "鉴权错误才该清缓存重新取")


if __name__ == "__main__":
    unittest.main()
