"""第 2 批回归：重推安全、静默失败、链接安全、截断兜底、台账口径。"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.db import CST, Database  # noqa: E402
from infotwitter.history import day_summary, render_text  # noqa: E402
from infotwitter.render import build_push_text, build_report  # noqa: E402


def _cfg(tmp: Path):
    from infotwitter.config import load_config

    base = load_config()
    data = copy.deepcopy(base._data)
    data.setdefault("app", {})
    data["app"]["db_path"] = str(tmp / "t.db")
    data["app"]["report_dir"] = str(tmp / "reports")
    data["app"]["log_dir"] = str(tmp / "logs")
    data.setdefault("push", {})
    data["push"]["channel"] = "console"
    return type(base)(data, base.sources)


class _FailingNotifier:
    name = "failing"
    content_type = "text"
    max_bytes = 20000

    def send(self, title, text):
        return False, "network down"


class TestDeliveredRowsAreImmutable(unittest.TestCase):
    """P1-5：已送达的台账行不能被 --force 重推改写。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")

    def tearDown(self):
        self.db.close()

    def _row(self, payload, sent_at, delivery_ok):
        return {"push_date": "2026-11-18", "slot": 1, "story_id": None, "title": "t",
                "channel": "pushplus", "payload": payload, "sent_at": sent_at,
                "delivery_ok": delivery_ok}

    def test_delivered_row_survives_force(self):
        self.db.record_push(self._row("OLD", "2026-11-18T12:04:50+08:00", 1))
        self.db.begin_push_batch(
            [{"push_date": "2026-11-18", "slot": 1, "story_id": None, "title": "t2",
              "channel": "pushplus", "payload": "NEW"}],
            "2026-11-18T12:49:00+08:00",
        )
        row = self.db.conn.execute(
            "SELECT payload, sent_at, delivery_ok, title FROM pushes WHERE push_date='2026-11-18'"
        ).fetchone()
        self.assertEqual(row["payload"], "OLD", "读者收到的那一份不能被改写")
        self.assertEqual(row["sent_at"], "2026-11-18T12:04:50+08:00")
        self.assertEqual(row["title"], "t")
        self.assertEqual(row["delivery_ok"], 1)

    def test_failed_row_is_replaceable(self):
        self.db.begin_push_batch(
            [{"push_date": "2026-11-18", "slot": 1, "story_id": None, "title": "t",
              "channel": "pushplus", "payload": "OLD"}], "2026-11-18T12:00:00+08:00")
        self.db.finish_push_batch("2026-11-18T12:00:00+08:00", False)
        self.db.begin_push_batch(
            [{"push_date": "2026-11-18", "slot": 1, "story_id": None, "title": "t",
              "channel": "pushplus", "payload": "NEW"}], "2026-11-18T12:30:00+08:00")
        row = self.db.conn.execute("SELECT payload FROM pushes WHERE push_date='2026-11-18'").fetchone()
        self.assertEqual(row["payload"], "NEW", "失败的行应该允许被重试覆盖")


class TestForceReprintStaging(unittest.TestCase):
    """P1-5：重推失败时，读者看到的站点报告必须原样保留。"""

    def test_force_does_not_touch_site_before_success(self):
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        cfg = _cfg(tmp)
        today = datetime.now(CST).date().isoformat()

        cfg.report_dir.mkdir(parents=True, exist_ok=True)
        site = cfg.report_dir / f"{today}.html"
        site.write_text("ORIGINAL", encoding="utf-8")

        db = Database(cfg.db_path)
        db.record_push({"push_date": today, "slot": 1, "story_id": None, "title": "already",
                        "channel": "pushplus", "payload": "ORIGINAL", "delivery_ok": 1})
        db.close()

        with mock.patch("infotwitter.runner.build_notifier", return_value=_FailingNotifier()):
            run_daily(cfg, skip_collect=True, force=True)

        self.assertEqual(site.read_text(encoding="utf-8"), "ORIGINAL",
                         "--force 失败后站点根必须保持原样")
        db = Database(cfg.db_path)
        row = db.conn.execute("SELECT payload FROM pushes WHERE push_date=?", (today,)).fetchone()
        self.assertEqual(row["payload"], "ORIGINAL", "台账 payload 不能被改写")
        db.close()


class TestLockedExitCode(unittest.TestCase):
    """P1-6：锁冲突必须返回非 0，否则任务计划程序永远显示成功。"""

    def test_locked_returns_4(self):
        import argparse

        from infotwitter import cli

        args = argparse.Namespace(dry_run=False, no_push=False, force=False,
                                    reanalyze=False, no_collect=True)
        with mock.patch("infotwitter.cli.run_daily", return_value={"status": "locked"}):
            self.assertEqual(cli.cmd_run(args, None), 4)


class TestHistoryStates(unittest.TestCase):
    """P2-5：在途（delivery_ok=2）不能算作已送达。"""

    def _rows(self):
        return [
            {"push_date": "2026-11-18", "slot": 1, "story_id": "a", "title": "ok",
             "importance": 60, "delivery_ok": 1, "authenticity": "L0"},
            {"push_date": "2026-11-18", "slot": 2, "story_id": "b", "title": "in-flight",
             "importance": 55, "delivery_ok": 2, "authenticity": "L1"},
            {"push_date": "2026-11-18", "slot": 3, "story_id": "c", "title": "failed",
             "importance": 50, "delivery_ok": 0, "authenticity": "L1"},
        ]

    def test_pending_not_counted_as_delivered(self):
        day = day_summary(self._rows())["2026-11-18"]
        self.assertEqual(day["ok"], 1)
        self.assertEqual(day["pending"], 1)

    def test_pending_marked_in_listing(self):
        text = render_text(self._rows(), 3)
        self.assertIn("[在途/未确认]", text)
        self.assertIn("[未送达]", text)


class TestSafeLinksInReport(unittest.TestCase):
    """P2-3：报告页的 href 必须过 scheme 白名单。"""

    def test_javascript_url_not_rendered(self):
        slots = {
            "mode": "normal",
            "general": [{"id": "s1", "canonical_title": "x", "title_zh": "x",
                         "primary_url": "javascript:alert(1)", "importance_total": 60,
                         "score_breakdown": "{}", "authenticity": "L1",
                         "event_type": "game", "summary": "s", "why_matters": "w"}],
            "product": [], "product_rows": [], "cn": None,
        }
        path = build_report(slots, "2026-11-18", Path(tempfile.mkdtemp()), run_stats={},
                            llm_stats={}, failed_sources=[], source_names={},
                            effective_threshold=60, llm_enabled=True, write_latest=False)
        html = path.read_text(encoding="utf-8")
        self.assertNotIn("javascript:", html)
        self.assertNotIn('href=""', html)


class TestTruncateKeepsLink(unittest.TestCase):
    """P2-4：截断必须保住「完整版」链接。"""

    def test_link_survives_small_budget(self):
        url = "http://host/2026-11-18.html"
        general = [
            {"id": f"s{i}", "canonical_title": "很长的标题" * 3, "title_zh": "很长的标题" * 3,
             "importance_total": 60, "authenticity": "L1", "event_type": "game",
             "why_matters": "要点" * 20, "summary": "摘要" * 40,
             "published_at": "2026-11-18T00:00:00+08:00"}
            for i in range(5)
        ]
        slots = {"mode": "normal", "general": general, "product": [], "product_rows": [], "cn": None}
        for cap in (2048, 1200):
            text = build_push_text(slots, "2026-11-18", url, max_bytes=cap)
            self.assertIn(url, text, f"上限 {cap} 时丢了完整版链接")
            self.assertLessEqual(len(text.encode("utf-8")), cap)


if __name__ == "__main__":
    unittest.main()

class _OkNotifier:
    name = "stub"
    content_type = "text"
    max_bytes = 20000

    def send(self, title, text):
        return True, "ok"


class TestForceReprintPublishesOnSuccess(unittest.TestCase):
    """P1-5 的另一半：重推成功才覆盖站点根，并留下 republished 记录。"""

    def test_success_replaces_site_report(self):
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        cfg = _cfg(tmp)
        today = datetime.now(CST).date().isoformat()
        cfg.report_dir.mkdir(parents=True, exist_ok=True)
        site = cfg.report_dir / f"{today}.html"
        site.write_text("ORIGINAL", encoding="utf-8")

        db = Database(cfg.db_path)
        db.record_push({"push_date": today, "slot": 1, "story_id": None, "title": "already",
                        "channel": "stub", "payload": "ORIGINAL", "delivery_ok": 1})
        db.close()

        slots = {
            "mode": "normal",
            "general": [{"id": "s1", "canonical_title": "新内容", "title_zh": "新内容",
                         "importance_total": 70.0, "authenticity": "L0", "event_type": "mini",
                         "primary_url": "https://x.example/a", "summary": "摘要",
                         "why_matters": "要点", "score_breakdown": "{}",
                         "published_at": datetime.now(CST).replace(microsecond=0).isoformat()}],
            "product": [], "product_rows": [], "cn": None, "price_coverage": {},
        }
        with mock.patch("infotwitter.runner.select_slots", return_value=slots), \
                mock.patch("infotwitter.runner.build_notifier", return_value=_OkNotifier()), \
                mock.patch("infotwitter.runner.ensure_running", return_value=True):
            stats = run_daily(cfg, skip_collect=True, force=True)

        self.assertTrue(stats.get("push", {}).get("ok"))
        self.assertIn("republished", stats)
        self.assertNotEqual(site.read_text(encoding="utf-8"), "ORIGINAL",
                            "重推成功后站点报告应该更新")
        # 台账里"读者当时收到的那一份"仍然不可变
        db = Database(cfg.db_path)
        row = db.conn.execute("SELECT payload FROM pushes WHERE push_date=?", (today,)).fetchone()
        self.assertEqual(row["payload"], "ORIGINAL")
        db.close()
