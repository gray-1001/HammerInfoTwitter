# -*- coding: utf-8 -*-
"""回归：build_report 不能因为 failed_sources 的类型问题炸掉整条推送。

背景（2026-11-21 真实事故）：runner 在正文抓取告警里写了
`failed = int(fetch_stats["failed"])`，把外层的「失败源列表」覆盖成整数；
渲染时 `[names.get(f, f) for f in failed_sources]` 抛
`TypeError: 'int' object is not iterable`，整轮在推送前崩掉，
当天没有日报也没有推送，任务退出码 1。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.render import build_report  # noqa: E402


def _slots() -> dict:
    return {
        "mode": "normal",
        "general": [{
            "id": "s1", "canonical_title": "Test headline", "title_zh": "测试标题",
            "importance_total": 66.0, "authenticity": "L0", "source_ids": ["whc_news"],
            "category": "40k", "impact_scope": "global", "event_type": "rules",
            "score_breakdown": "{}", "summary": "摘要", "why_matters": "为什么重要",
            "primary_url": "https://example.com/a", "first_seen_at": "2026-11-21T10:00:00+08:00",
        }],
        "product": [], "product_rows": [], "cn": None,
    }


class BuildReportFailedSourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build(self, failed_sources):
        return build_report(
            _slots(), "2026-11-21", self.out,
            run_stats={"collected": 1, "inserted": 1, "stories": 1, "skipped_collect": False},
            llm_stats={"provider": "deepseek", "calls": 1, "tokens": 100, "failures": 0},
            failed_sources=failed_sources,
            source_names={"whc_news": "Warhammer Community"},
            write_latest=False,
        )

    def test_list_of_ids_renders(self):
        path = self._build(["gnews_40k", "reddit_40k"])
        self.assertTrue(path.exists())
        self.assertIn("Test headline", path.read_text(encoding="utf-8"))

    def test_empty_list_renders(self):
        path = self._build([])
        self.assertTrue(path.exists())

    def test_wrong_type_does_not_crash(self):
        """即使上游传错类型，也不能让整轮推送失败（只丢这个展示字段）。"""
        path = self._build(3)
        self.assertTrue(path.exists(), "failed_sources 类型异常时仍应产出日报")

    def test_none_does_not_crash(self):
        path = self._build(None)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
