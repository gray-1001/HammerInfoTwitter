"""抓取深度修复的回归测试：Google News 还原轮询、Reddit 正文、老行正文升级。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter.db import Database  # noqa: E402

LONG_PROSE = "This is a real article body. " * 30
SHORT_TEXT = "Just a moment..."


class _FakePage:
    """模拟 Playwright 页面：第一次等完就从 goog 跳到发布方。"""

    def __init__(self, land_on: str, html: str, hops: int = 1):
        self._land = land_on
        self._html = html
        self._hops = hops
        self._waits = 0
        self.url = "https://news.google.com/rss/articles/CBMiXXX"
        self.goto_calls = 0

    def goto(self, url, **kw):
        self.goto_calls += 1
        self.url = url

    def wait_for_timeout(self, ms):
        self._waits += 1
        if self._waits > self._hops:
            self.url = self._land

    def content(self):
        return f"<html><body><p>{self._html}</p></body></html>"

    def evaluate(self, *a, **k):
        return ""


class TestGoogleNewsResolve(unittest.TestCase):
    """C-16：跳转要轮询，失败也要记住真实地址。"""

    def test_polls_until_redirect(self):
        from infotwitter.content import _resolve_and_fetch

        page = _FakePage("https://example.com/post", LONG_PROSE, hops=1)
        resolved = {}
        text = _resolve_and_fetch(page, page.url, 2500, resolved=resolved)
        self.assertTrue(text, "跳到发布方后应该抽到正文")
        self.assertEqual(resolved.get("https://news.google.com/rss/articles/CBMiXXX"),
                         "https://example.com/post")

    def test_records_resolved_when_text_fails(self):
        from infotwitter.content import _resolve_and_fetch

        page = _FakePage("https://example.com/blocked", SHORT_TEXT, hops=1)
        resolved = {}
        text = _resolve_and_fetch(page, page.url, 2500, resolved=resolved)
        self.assertEqual(text, "", "抽不到正文就返回空")
        self.assertEqual(resolved.get("https://news.google.com/rss/articles/CBMiXXX"),
                         "https://example.com/blocked",
                         "至少要记住真实地址，下一轮才能直接 HTTP 抓")

    def test_never_redirects_gives_nothing(self):
        from infotwitter.content import _resolve_and_fetch

        page = _FakePage("https://news.google.com/still", LONG_PROSE, hops=999)
        resolved = {}
        text = _resolve_and_fetch(page, page.url, 800, resolved=resolved)
        self.assertEqual(text, "")
        self.assertEqual(resolved, {}, "没跳走就不能瞎记地址")


class TestBodyUpgrade(unittest.TestCase):
    """C-17：适配器后来学会抓正文，老行必须能升级并重新排队分析。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        self.db.conn.execute(
            "INSERT INTO sources (id,name,url,category,tier,lang,fetch_method)"
            " VALUES ('reddit_40k','R','u','community','L2','en','reddit')"
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()

    def _doc(self, body, title="t"):
        return {"source_id": "reddit_40k", "url": "https://reddit.com/x",
                "title": title, "body": body, "content_hash": "h1",
                "published_at": "2026-11-18T10:00:00+08:00", "time_source": "article"}

    def test_short_body_not_overwritten(self):
        self.db.insert_raw_docs([self._doc("标题（Reddit r/Warhammer40k）")])
        self.db.insert_raw_docs([self._doc("标题（Reddit r/Warhammer40k）改了一点点")])
        row = self.db.conn.execute("SELECT body FROM raw_docs").fetchone()
        self.assertIn("Reddit", row["body"])
        self.assertNotIn("改了一点点", row["body"], "小幅差异不该反复重写")

    def test_longer_body_upgrades_and_requeues(self):
        self.db.insert_raw_docs([self._doc("标题（Reddit r/Warhammer40k）")])
        self.db.conn.execute("UPDATE raw_docs SET analyzed_at='2026-11-18T10:05:00+08:00'")
        self.db.conn.commit()
        self.db.insert_raw_docs([self._doc("正文" * 300)])
        row = self.db.conn.execute("SELECT body, analyzed_at FROM raw_docs").fetchone()
        self.assertGreater(len(row["body"]), 400)
        self.assertIsNone(row["analyzed_at"], "正文升级后要重新分析")


class TestRedditTextPosts(unittest.TestCase):
    """C-17：只给文本帖抓正文，图片/图集帖不白跑。"""

    def test_build_docs_marks_text_posts(self):
        from datetime import datetime, timedelta, timezone

        from infotwitter.adapters.reddit import RedditAdapter
        from infotwitter.db import CST

        since = datetime.now(CST) - timedelta(hours=168)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        adapter = RedditAdapter()
        docs = adapter._build_docs(
            {"id": "reddit_40k"},
            [
                {"href": "https://www.reddit.com/r/Warhammer40k/comments/aaa/x/",
                 "title": "有正文的帖子标题", "ts": now, "kind": "text"},
                {"href": "https://www.reddit.com/r/Warhammer40k/comments/bbb/y/",
                 "title": "只发了一张图", "ts": now, "kind": "image"},
            ],
            "Warhammer40k", since, 40,
        )
        flags = {d["title"]: d["_has_body"] for d in docs}
        self.assertTrue(flags["有正文的帖子标题"])
        self.assertFalse(flags["只发了一张图"])


if __name__ == "__main__":
    unittest.main()
