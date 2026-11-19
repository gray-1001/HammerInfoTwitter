"""第 2 批：通道与采集失败时的可见性。"""
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

from infotwitter.db import CST  # noqa: E402


def _cfg(tmp: Path, push_dry_run: bool = False):
    from infotwitter.config import load_config

    base = load_config()
    data = copy.deepcopy(base._data)
    data.setdefault("app", {})
    data["app"]["db_path"] = str(tmp / "t.db")
    data["app"]["report_dir"] = str(tmp / "reports")
    data["app"]["log_dir"] = str(tmp / "logs")
    data.setdefault("push", {})
    data["push"]["channel"] = "pushplus"
    data["push"]["pushplus_token"] = "x" * 32
    data["push"]["dry_run"] = push_dry_run
    return type(base)(data, base.sources)


def _fake_slots():
    """固定的选槽结果：绕开聚类/评分，只验证「有内容要推」之后的路径。"""
    item = {
        "id": "seed-story", "canonical_title": "Seeded news", "title_zh": "种子新闻",
        "importance_total": 70.0, "authenticity": "L0", "event_type": "mini",
        "primary_url": "https://seed.example/a", "summary": "摘要", "why_matters": "要点",
        "published_at": datetime.now(CST).replace(microsecond=0).isoformat(),
        "source_ids": ["seed"], "score_breakdown": "{}",
    }
    return {"mode": "normal", "general": [item], "product": [],
            "product_rows": [], "cn": None, "price_coverage": {}}


class TestPreviewChannelDoesNotRecord(unittest.TestCase):
    """P1-6：push.dry_run=true 只预览，不能写台账、不能算已推送。"""

    def test_preview_marks_failure_and_writes_nothing(self):
        from infotwitter.db import Database
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        cfg = _cfg(tmp, push_dry_run=True)
        today = datetime.now(CST).date().isoformat()

        with mock.patch("infotwitter.runner.select_slots", return_value=_fake_slots()):
            stats = run_daily(cfg, skip_collect=True)

        push = stats.get("push")
        self.assertIsInstance(push, dict, f"push 应为 dict，实际 {push!r}")
        self.assertFalse(push["ok"], "预览不能被当成发送成功")

        db = Database(cfg.db_path)
        n = db.conn.execute("SELECT COUNT(*) c FROM pushes WHERE push_date=?", (today,)).fetchone()["c"]
        self.assertEqual(n, 0, "预览不得写台账")
        self.assertFalse(db.pushed_today(today))
        db.close()

    def test_alert_written_on_lock(self):
        """锁冲突要留痕到 alert.log，不能只打一行日志。"""
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        cfg = _cfg(tmp)
        lock = cfg.db_path.parent / ".run.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("held", encoding="utf-8")

        stats = run_daily(cfg, skip_collect=True)
        self.assertEqual(stats.get("status"), "locked")
        alert = cfg.log_dir / "alert.log"
        self.assertTrue(alert.exists(), "锁冲突必须写 alert.log")
        self.assertIn("运行锁被占用", alert.read_text(encoding="utf-8"))


class TestAllSourcesFailed(unittest.TestCase):
    """P1-6：全部源失败时不能说「运行成功」，也不能拿旧内容去推。"""

    def test_collect_dead_blocks_push(self):
        from infotwitter.runner import run_daily

        tmp = Path(tempfile.mkdtemp())
        cfg = _cfg(tmp)
        all_ids = [s["id"] for s in cfg.sources if s.get("active", True)]

        with mock.patch("infotwitter.runner.collect", return_value=([], all_ids)), \
                mock.patch("infotwitter.runner.select_slots", return_value=_fake_slots()):
            stats = run_daily(cfg, skip_collect=False, skip_push=False)

        self.assertTrue(stats.get("collect_dead"))
        push = stats.get("push")
        self.assertIsInstance(push, dict)
        self.assertFalse(push["ok"])
        alert = cfg.log_dir / "alert.log"
        self.assertTrue(alert.exists())
        self.assertIn("采集失败", alert.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
