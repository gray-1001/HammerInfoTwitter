"""项目入口：python run.py <command>"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# pythonw.exe 启动时 sys.stdout / sys.stderr 为 None，
# 而 CLI 里会 print 与 reconfigure，直接崩掉。落到日志文件里保证服务能常驻。
if sys.stdout is None or sys.stderr is None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = open(log_dir / "service.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
else:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from infotwitter.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
