"""第 2 批回归测试：D-02 幂等前移、D-03 在途占位与锁、D-08 doctor、D-09 通道、D-11 台账不降级。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from infotwitter.db import Database  # noqa: E402
from infotwitter.doctor import FAIL, OK, WARN, _check_push  # noqa: E402
from infotwitter.runner import RunLock, _push_rows  # noqa: E402


class _Cfg:
    """只实现 doctor 用到的那几个 get。"""

    def __init__(self, data: dict):
        self._d = data

    def get(self, key: str, default=None):
        return self._d.get(key, default)


class TestPushBatch(unittest.TestCase):
    """D-03：先占位（2）再确认（1/0）。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")

    def tearDown(self):
        self.db.close()

    def _rows(self, today="2026-11-18"):
        return [{"push_date": today, "slot": 1, "story_id": None, "title": "t",
                 "importance": 60, "channel": "pushplus", "payload": "p"}]

    def test_pending_then_delivered(self):
        sent = "2026-11-18T12:00:00+08:00"
        self.db.begin_push_batch(self._rows(), sent)
        self.assertEqual(self.db.conn.execute(
            "SELECT delivery_ok FROM pushes").fetchone()["delivery_ok"], 2)
        self.assertFalse(self.db.pushed_today("2026-11-18"))
        self.assertTrue(self.db.pending_today("2026-11-18"), "在途必须可查")

        self.db.finish_push_batch(sent, ok=True)
        self.assertTrue(self.db.pushed_today("2026-11-18"))
        self.assertFalse(self.db.pending_today("2026-11-18"))

    def test_failed_batch_allows_retry(self):
        sent = "2026-11-18T12:00:00+08:00"
        self.db.begin_push_batch(self._rows(), sent)
        self.db.finish_push_batch(sent, ok=False)
        self.assertFalse(self.db.pushed_today("2026-11-18"), "失败要允许下次重试")
        self.assertFalse(self.db.pending_today("2026-11-18"))

    def test_story_status_follows_result(self):
        self.db.conn.execute(
            "INSERT INTO stories (id, canonical_title, first_seen_at, last_seen_at, status)"
            " VALUES ('s1','t','2026-11-18T10:00:00+08:00','2026-11-18T10:00:00+08:00','scored')"
        )
        self.db.conn.commit()
        self.db.set_stories_status(["s1"], "pushed")
        self.assertEqual(self.db.conn.execute(
            "SELECT status FROM stories WHERE id='s1'").fetchone()["status"], "pushed")


class TestLedgerNotDowngraded(unittest.TestCase):
    """D-11：已成功送达的行不能被后来的失败覆盖。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")

    def tearDown(self):
        self.db.close()

    def _push(self, ok, slot=1, day="2026-11-18"):
        self.db.record_push({"push_date": day, "slot": slot, "story_id": None,
                             "title": "t", "channel": "pushplus", "payload": "p",
                             "delivery_ok": 1 if ok else 0})

    def test_success_survives_later_failure(self):
        self._push(True)
        self._push(False)
        self.assertTrue(self.db.pushed_today("2026-11-18"), "成功记录不得被失败覆盖")

    def test_failure_can_become_success(self):
        self._push(False)
        self._push(True)
        self.assertTrue(self.db.pushed_today("2026-11-18"))


class TestPushRows(unittest.TestCase):
    """D-01 的回归：只有价格表时也必须产出槽 5 行。"""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")

    def tearDown(self):
        self.db.close()

    def test_price_only_produces_slot5(self):
        rows, _ = _push_rows(self.db, "2026-11-18",
                             {"general": [], "product": [], "cn": None,
                              "product_rows": [{"id": "39-13"}]}, "p", "pushplus")
        self.assertEqual([r["slot"] for r in rows], [5])

    def test_nothing_still_produces_placeholder(self):
        rows, _ = _push_rows(self.db, "2026-11-18",
                             {"general": [], "product": [], "cn": None, "product_rows": []},
                             "p", "pushplus")
        self.assertEqual([r["slot"] for r in rows], [0])


class TestRunLock(unittest.TestCase):
    """D-03：手工重跑与计划任务不能同时发。"""

    def test_second_acquire_fails(self):
        path = Path(tempfile.mkdtemp()) / ".run.lock"
        first = RunLock(path)
        second = RunLock(path)
        first.acquire()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire()
        finally:
            first.release()
        # 释放后可以再拿
        third = RunLock(path)
        third.acquire()
        third.release()

    def test_stale_lock_is_taken_over(self):
        path = Path(tempfile.mkdtemp()) / ".run.lock"
        path.write_text("99999 old", encoding="utf-8")
        old = time.time() - 10_000
        import os

        os.utime(path, (old, old))
        lock = RunLock(path, stale_seconds=60)
        lock.acquire()
        self.assertTrue(lock.acquired, "遗留锁应被自动接管")
        lock.release()
        self.assertFalse(path.exists())


class TestDoctorPush(unittest.TestCase):
    """D-08：dry_run 误配要判 FAIL；配置好但未验证仍是 WARN。"""

    def _cfg(self, **kw):
        base = {"push.channel": "pushplus", "push.pushplus_token": "x" * 32,
                "push.dry_run": False, "push.pushplus_template": "html"}
        base.update(kw)
        return _Cfg(base)

    def test_dry_run_is_fail(self):
        c = _check_push(self._cfg(**{"push.dry_run": True}))
        self.assertEqual(c.status, FAIL, "正式配置开着 dry_run 等于不推送")

    def test_no_token_is_fail(self):
        import os

        saved = os.environ.pop("PUSHPLUS_TOKEN", None)
        try:
            c = _check_push(self._cfg(**{"push.pushplus_token": ""}))
            self.assertEqual(c.status, FAIL, "配置与环境变量都没有 token 时必须 FAIL")
        finally:
            if saved is not None:
                os.environ["PUSHPLUS_TOKEN"] = saved

    def test_configured_but_unverified_is_warn(self):
        c = _check_push(self._cfg())
        self.assertEqual(c.status, WARN)
        self.assertIn("--probe-push", c.detail)


class TestAlreadyPushedDoesNotRerender(unittest.TestCase):
    """D-02：已推送的日子再跑一次，不能覆盖已发布的日报。"""

    def test_report_is_not_rewritten(self):
        import copy
        from datetime import datetime

        from infotwitter.config import load_config
        from infotwitter.db import CST
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        base = load_config()
        data = copy.deepcopy(base._data)
        data.setdefault("app", {})
        data["app"]["db_path"] = str(tmp / "t.db")
        data["app"]["report_dir"] = str(tmp / "reports")
        data["app"]["log_dir"] = str(tmp / "logs")
        data.setdefault("push", {})
        data["push"]["channel"] = "console"
        cfg = type(base)(data, base.sources)

        today = datetime.now(CST).date().isoformat()
        db = Database(cfg.db_path)
        db.record_push({"push_date": today, "slot": 1, "story_id": None, "title": "already",
                        "channel": "pushplus", "payload": "p", "delivery_ok": 1})
        db.close()

        stats = run_daily(cfg, skip_collect=True)
        self.assertEqual(stats.get("push"), "already_pushed")
        self.assertFalse((cfg.report_dir / f"{today}.html").exists(),
                         "已推送的日子不应重写日报页")

    def test_no_push_preview_writes_to_dryrun(self):
        """--no-push 预览已推送的日子时，只能写 _dryrun，不能覆盖站点根。"""
        import copy
        from datetime import datetime

        from infotwitter.config import load_config
        from infotwitter.db import CST
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        base = load_config()
        data = copy.deepcopy(base._data)
        data.setdefault("app", {})
        data["app"]["db_path"] = str(tmp / "t.db")
        data["app"]["report_dir"] = str(tmp / "reports")
        data["app"]["log_dir"] = str(tmp / "logs")
        cfg = type(base)(data, base.sources)

        today = datetime.now(CST).date().isoformat()
        db = Database(cfg.db_path)
        db.record_push({"push_date": today, "slot": 1, "story_id": None, "title": "already",
                        "channel": "pushplus", "payload": "p", "delivery_ok": 1})
        db.close()

        run_daily(cfg, skip_collect=True, skip_push=True)
        self.assertFalse((cfg.report_dir / f"{today}.html").exists(),
                         "站点根不得被预览覆盖")
        self.assertFalse((cfg.report_dir / "latest.html").exists())
        self.assertTrue((cfg.report_dir / "_dryrun" / f"{today}.html").exists(),
                        "预览应落到 _dryrun")

    def test_pending_row_also_blocks(self):
        """在途记录（上次可能没确认送达）同样要挡住重发。"""
        import copy
        from datetime import datetime

        from infotwitter.config import load_config
        from infotwitter.db import CST
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        base = load_config()
        data = copy.deepcopy(base._data)
        data.setdefault("app", {})
        data["app"]["db_path"] = str(tmp / "t.db")
        data["app"]["report_dir"] = str(tmp / "reports")
        data["app"]["log_dir"] = str(tmp / "logs")
        data.setdefault("push", {})
        data["push"]["channel"] = "console"
        cfg = type(base)(data, base.sources)

        today = datetime.now(CST).date().isoformat()
        db = Database(cfg.db_path)
        # 关键：在途记录必须是「刚发出」的。超时的在途会被状态机收敛成失败，
        # 那是 2026-11-19 加的恢复语义（进程被杀后当天仍可重推），不是漏挡。
        fresh = datetime.now(CST).replace(microsecond=0).isoformat()
        db.begin_push_batch([{"push_date": today, "slot": 1, "story_id": None, "title": "x",
                              "channel": "pushplus", "payload": "p"}], fresh)
        db.close()

        stats = run_daily(cfg, skip_collect=True)
        self.assertEqual(stats.get("push"), "already_pushed")

    def test_stale_pending_row_does_not_block_forever(self):
        """超时的在途记录必须被收敛成失败，否则当天会永远静默不推（P-01）。"""
        import copy
        from datetime import datetime, timedelta

        from infotwitter.config import load_config
        from infotwitter.db import CST
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        base = load_config()
        data = copy.deepcopy(base._data)
        data.setdefault("app", {})
        data["app"]["db_path"] = str(tmp / "t.db")
        data["app"]["report_dir"] = str(tmp / "reports")
        data["app"]["log_dir"] = str(tmp / "logs")
        data.setdefault("push", {})
        data["push"]["channel"] = "console"
        cfg = type(base)(data, base.sources)

        today = datetime.now(CST).date().isoformat()
        stale = (datetime.now(CST) - timedelta(hours=6)).replace(microsecond=0).isoformat()
        db = Database(cfg.db_path)
        db.begin_push_batch([{"push_date": today, "slot": 1, "story_id": None, "title": "x",
                              "channel": "pushplus", "payload": "p"}], stale)
        db.close()

        stats = run_daily(cfg, skip_collect=True)
        self.assertNotEqual(stats.get("push"), "already_pushed",
                            "超时在途不能再挡住当天投递")


if __name__ == "__main__":
    unittest.main()
