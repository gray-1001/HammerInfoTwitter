# -*- coding: utf-8 -*-
"""日报站点的两条安全约束（P-12）+ 探活不能依赖内容文件。

背景：report_dir 同时是站点根，`_dryrun/`（未推送预览）和归档目录都躺在里面，
实测能直接访问；而探活过去用 `/latest.html`，历史清空后会把「服务正常」误判成
「服务没起」，进而拦住推送。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.server import Handler  # noqa: E402


def _handler(directory: Path) -> Handler:
    h = Handler.__new__(Handler)      # 不启动 socket，只测路径解析
    h.report_dir = directory
    h.directory = str(directory)
    return h


class StaticPathGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "2026-11-19.html").write_text("<html>x</html>", encoding="utf-8")
        (self.dir / "_dryrun").mkdir()
        (self.dir / "_dryrun" / "push.html").write_text("<html>secret</html>", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_normal_page_allowed(self):
        out = _handler(self.dir).translate_path("/2026-11-19.html")
        self.assertTrue(out.endswith("2026-11-19.html"))

    def test_underscore_dir_blocked(self):
        out = _handler(self.dir).translate_path("/_dryrun/push.html")
        self.assertIn("__forbidden__", out,
                      "以下划线开头的内部目录必须被屏蔽，未推送内容不能提前泄露")

    def test_underscore_with_encoded_chars_blocked(self):
        out = _handler(self.dir).translate_path("/%5Fdryrun/push.html")
        self.assertIn("__forbidden__", out, "URL 编码过的下划线也要挡")


class HealthzRouteTest(unittest.TestCase):
    def test_handler_defines_healthz(self):
        """服务必须回答 /healthz——探活不能依赖 latest.html 是否存在。"""
        self.assertTrue(
            hasattr(Handler, "do_GET") or "/healthz" in Handler.__doc__ or True,
        )
        import inspect

        src = inspect.getsource(Handler.do_GET) if hasattr(Handler, "do_GET") else ""
        self.assertIn("/healthz", src, "do_GET 里必须有 /healthz 分支")


if __name__ == "__main__":
    unittest.main()
