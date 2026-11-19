"""NGA 适配器测试：HTML 解析、时间窗过滤、未登录行为。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.adapters import nga  # noqa: E402
from infotwitter.db import CST  # noqa: E402

# 真实版块页的最小结构：.topicrow 里既有标题链接，也有 10 位 unix 时间戳
TS = int(datetime(2026, 11, 17, 20, 0, tzinfo=CST).timestamp())
SAMPLE = f"""
<div id="topicrows">
  <div class="topicrow">
    <a class="topic" href="/read.php?tid=47925608">《灰烬公报》新人涅克洛蒙达战报</a>
    <span class="c3">回复 3</span><span>{TS}</span>
  </div>
  <div class="topicrow">
    <a href="/read.php?tid=47899969">战锤幸存者，40k粉还是能玩的</a>
    <span>{TS - 86400 * 10}</span>
  </div>
  <div class="topicrow">
    <a href="/thread.php?fid=414">不是帖子的链接</a>
  </div>
</div>
"""


class TestParseBoard(unittest.TestCase):
    def test_extracts_tid_title_and_timestamp(self):
        threads = nga.parse_board(SAMPLE)
        self.assertEqual(len(threads), 2, "只认 read.php?tid= 的行")
        self.assertEqual(threads[0]["tid"], "47925608")
        self.assertIn("灰烬公报", threads[0]["title"])
        self.assertEqual(threads[0]["lastpost"], TS)

    def test_missing_timestamp_is_none(self):
        html = '<div class="topicrow"><a href="/read.php?tid=1">标题够长了吧</a></div>'
        threads = nga.parse_board(html)
        self.assertIsNone(threads[0]["lastpost"])


class TestToDocs(unittest.TestCase):
    def setUp(self):
        self.source = {"id": "nga_40k", "board": "战锤40K"}

    def test_window_filter(self):
        now = datetime.now(CST)
        recent = int((now - timedelta(hours=10)).timestamp())
        old = int((now - timedelta(days=30)).timestamp())
        threads = [
            {"tid": "1", "title": "新帖标题", "lastpost": recent},
            {"tid": "2", "title": "老帖标题", "lastpost": old},
        ]
        docs = nga.to_docs(self.source, threads, now - timedelta(hours=168))
        self.assertEqual([d["url"] for d in docs], ["https://ngabbs.com/read.php?tid=1"])

    def test_doc_shape(self):
        now = datetime.now(CST)
        threads = [{"tid": "9", "title": "测试帖", "lastpost": int(now.timestamp())}]
        doc = nga.to_docs(self.source, threads, now - timedelta(hours=168))[0]
        self.assertEqual(doc["source_id"], "nga_40k")
        self.assertEqual(doc["publisher_domain"], "ngabbs.com")
        self.assertEqual(doc["time_source"], "article")
        self.assertTrue(doc["content_hash"])


class TestLoginGuard(unittest.TestCase):
    def test_fetch_returns_none_without_login(self):
        with mock.patch.object(nga, "load_cookies", return_value={}):
            ad = nga.NgaAdapter()
            out = ad.fetch({"id": "nga_40k", "fid": "332"}, datetime.now(CST))
        self.assertIsNone(out, "未登录应返回 None（算失败），而不是空列表")

    def test_login_cookie_check(self):
        self.assertTrue(nga._has_login({"ngaPassportUid": "1", "ngaPassportCid": "2"}))
        self.assertFalse(nga._has_login({"ngaPassportUid": "1"}))
        self.assertFalse(nga._has_login({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
