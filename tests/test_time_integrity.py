"""第 0 批回归测试：时间来源完整性、3.11 兼容、doctor 判定。

对应核查报告：C-01（浏览器发布时间）、C-02（f-string 反斜杠）、A-07（doctor 假红）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.adapters import _parse_published_time, _published_or_fetch  # noqa: E402
from infotwitter.db import CST, Database  # noqa: E402
from infotwitter.pipeline import select_slots  # noqa: E402
from test_pipeline import make_config, make_story  # noqa: E402


class TestPublishedTimeParsing(unittest.TestCase):
    """C-01 的第一层：站点给的是文本日期（`Mon 16 Nov 26`），必须能解析。"""

    def test_warhammer_community_text_format(self):
        dt = _parse_published_time("Mon 16 Nov 26")
        self.assertIsNotNone(dt)
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 11, 16))

    def test_other_common_formats(self):
        for raw in ("16 Nov 2026", "16 November 2026", "Nov 16, 2026", "2026-11-16"):
            dt = _parse_published_time(raw)
            self.assertIsNotNone(dt, f"{raw} 应能解析")
            self.assertEqual((dt.month, dt.day), (11, 16))

    def test_machine_readable_formats(self):
        dt = _parse_published_time("2026-11-16T14:50:27+08:00")
        self.assertEqual(dt.hour, 14)

    def test_unparseable_returns_none(self):
        for raw in ("", "今天", "3 天前", "2026", None):
            self.assertIsNone(_parse_published_time(raw), f"{raw!r} 不应被解析")

    def test_published_or_fetch_never_fakes_now(self):
        """解析失败时必须返回 (None, 'fetch')，不能拿当前时间顶替。"""
        for raw in ("", "刚刚", "unknown"):
            published, source = _published_or_fetch(raw)
            self.assertIsNone(published)
            self.assertEqual(source, "fetch")

    def test_published_or_fetch_uses_article_source(self):
        published, source = _published_or_fetch("Mon 16 Nov 26")
        self.assertIsNotNone(published)
        self.assertEqual(source, "article")
        self.assertTrue(published.startswith("2026-11-16"))


class TestUndatedStoriesNotPushed(unittest.TestCase):
    """C-01 的第二层：时间未知的 story 不得进入「今日新闻」候选。"""

    def _story(self, sid, time_source, published):
        now = datetime.now(CST).replace(microsecond=0).isoformat()
        return make_story(
            id=sid, score=90, title=f"Headline about {sid} faction release",
            first_seen_at=now, published_at=published, time_source=time_source,
        )

    def test_undated_non_official_story_excluded(self):
        """非官方源缺发布时间 → 排除（C-01 的原意：防常青页面冒充新闻）。"""
        cfg = make_config()
        story = self._story("u1", "fetch", None)
        story["authenticity"] = "L2"
        slots = select_slots([story], cfg, set(), [])
        self.assertEqual(slots["general"], [], "时间未知的非官方 story 不能占通识槽")
        self.assertGreaterEqual(slots["stats"]["undated_skipped"], 1)

    def test_undated_official_story_also_excluded(self):
        """官方源（L0）拿不到发布日同样不进一级。

        曾经为了不让一级池空掉而「放行 L0」，结果把 10 月的旧文当成今天的头条
        （实测 2026-11-18：13/21/20 天前的三条进了槽 1–3）。
        现在日期可以从文章页 JSON-LD 回填（P0-1），守卫恢复严格。
        """
        cfg = make_config()
        story = self._story("u2", "fetch", None)
        story["authenticity"] = "L0"
        slots = select_slots([story], cfg, set(), [])
        self.assertEqual(slots["general"], [], "官方源无日期也不能当今日新闻")
        self.assertGreaterEqual(slots["stats"]["undated_skipped"], 1)

    def test_dated_story_still_selected(self):
        cfg = make_config()
        now = datetime.now(CST).replace(microsecond=0)
        published = (now - timedelta(hours=2)).isoformat()
        slots = select_slots([self._story("d1", "article", published)], cfg, set(), [])
        self.assertEqual(len(slots["general"]), 1)

    def test_old_published_story_only_fills_as_aged(self):
        """20 天前的文章：不进今日池，只能以 aged 身份补位（A-02 契约）。"""
        cfg = make_config()
        now = datetime.now(CST).replace(microsecond=0)
        published = (now - timedelta(days=20)).isoformat()
        slots = select_slots([self._story("d2", "article", published)], cfg, set(), [])
        self.assertNotEqual(slots["mode"], "normal", "不该当成正常今日新闻")
        for item in slots["general"]:
            self.assertEqual(item.get("_slot_reason"), "aged",
                             "旧文只能以补位身份出现")


class TestRawDocBackfill(unittest.TestCase):
    """C-01 的连带问题：已存在的行要能补回发布时间。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.tmp)
        self.db.conn.execute(
            "INSERT INTO sources (id,name,url,category,tier,lang,fetch_method) "
            "VALUES ('s1','S','u','official','L0','en','browser')"
        )
        self.db.conn.commit()

    def tearDown(self):
        self.db.close()

    def _doc(self, **kw):
        base = {
            "source_id": "s1", "url": "https://x/1", "title": "t",
            "content_hash": "h1", "fetched_at": "2026-11-17T14:50:27+08:00",
            "published_at": None, "time_source": "fetch",
        }
        base.update(kw)
        return base

    def test_backfills_published_at_on_conflict(self):
        self.db.insert_raw_docs([self._doc()])
        row = self.db.conn.execute("SELECT published_at, time_source FROM raw_docs").fetchone()
        self.assertIsNone(row["published_at"])

        # 适配器后来学会解析日期：同一 content_hash 再写一次
        self.db.insert_raw_docs([self._doc(published_at="2026-11-16T00:00:00+08:00",
                                           time_source="article")])
        row = self.db.conn.execute("SELECT published_at, time_source FROM raw_docs").fetchone()
        self.assertEqual(row["published_at"], "2026-11-16T00:00:00+08:00")
        self.assertEqual(row["time_source"], "article")

    def test_does_not_overwrite_existing_published_at(self):
        self.db.insert_raw_docs([self._doc(published_at="2026-11-10T00:00:00+08:00")])
        self.db.insert_raw_docs([self._doc(published_at="2026-11-16T00:00:00+08:00")])
        row = self.db.conn.execute("SELECT published_at FROM raw_docs").fetchone()
        self.assertEqual(row["published_at"], "2026-11-10T00:00:00+08:00", "已有值不应被覆盖")

    def test_dedup_still_works(self):
        self.db.insert_raw_docs([self._doc()])
        self.db.insert_raw_docs([self._doc()])
        n = self.db.conn.execute("SELECT COUNT(*) c FROM raw_docs").fetchone()["c"]
        self.assertEqual(n, 1, "同一 source_id + content_hash 不应产生重复行")


class TestPy311Compatibility(unittest.TestCase):
    """C-02：f-string 表达式内不允许反斜杠（PEP 701 之前的语法限制）。

    直接检查源码里有没有 `f"...{...\\...}..."` 这种写法——3.11 上会直接语法错误。
    """

    def test_no_backslash_inside_fstring_expression(self):
        import re as _re

        offenders = []
        for path in (ROOT / "src" / "infotwitter").rglob("*.py"):
            for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                stripped = line.strip()
                if not stripped.startswith(("f\"", "f'", "return f\"", "return f'")):
                    continue
                # 找出 f-string 的 {...} 片段，里面若有反斜杠就是隐患
                for expr in _re.findall(r"\{([^{}]*)\}", line):
                    if "\\" in expr:
                        offenders.append(f"{path.name}:{i}  {expr[:60]}")
        self.assertEqual(offenders, [], f"以下 f-string 在 Python 3.11 上会语法错误：{offenders}")


if __name__ == "__main__":
    unittest.main()
