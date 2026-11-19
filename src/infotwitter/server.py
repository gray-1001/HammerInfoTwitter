"""日报本地服务：静态页面 + 反馈接口。

企业微信群机器人是单向的（只能发不能收），所以反馈走日报页按钮：
点击 → POST /api/feedback → 写 feedback 表 → 更新兴趣向量。

安全边界（核查报告 BUG-25）：
- 默认仍然监听 0.0.0.0 —— 推送里的链接就是局域网地址，手机要能打开；
- 平台没有鉴权体系，因此用「按天轮换的令牌 + 同 story 同信号每天只计一次」
  挡住跨站伪造与连点刷权重；
- 关闭目录列表、限制请求体大小、错误细节只进日志不回传。
"""
from __future__ import annotations

import functools
import http.server
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .db import CST, now_iso
from .tokens import feedback_token, verify_feedback_token

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 4096

SIGNAL_INTEREST = {
    "useful": 0.10,
    "useless": -0.10,
    "bought": 0.20,
    "fake": 0.0,       # 假的：降来源层级，不降品类兴趣
    "duplicate": 0.0,  # 重复：只记错例，不影响兴趣
}


class Handler(http.server.SimpleHTTPRequestHandler):
    db_path: Path
    report_dir: Path

    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，避免刷屏
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---------- 安全 ----------
    def list_directory(self, path: str):  # noqa: A003 - 与基类签名一致
        """关闭目录列表：不要把 reports/ 里的全部文件名挂在网上。"""
        self.send_error(403, "Directory listing disabled")
        return None

    def translate_path(self, path: str) -> str:
        """屏蔽以下划线开头的内部目录。

        report_dir 同时是站点根，`_dryrun/`（未推送的预览）和
        `_archive_before_first_push/`（历史归档）都躺在里面。实测这两个目录
        在浏览器里能直接打开——没发出去的内容提前泄露（P-12）。
        """
        from urllib.parse import unquote, urlparse

        clean = urlparse(path).path
        parts = [p for p in unquote(clean).split("/") if p]
        if any(p.startswith("_") for p in parts):
            # 指向一个必然不存在的路径，让基类返回 404
            return str(Path(self.report_dir) / "__forbidden__")
        return super().translate_path(path)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 不再给 * 的跨域许可：反馈是页内同源请求，不需要 CORS
        self.send_response(204)
        self.send_header("Allow", "POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        """健康检查单独一条路由。

        探活不能用内容文件：latest.html 可能因为历史清空/首次部署而不存在，
        拿它当探针会把「服务正常、今天还没发布」误判成「服务没起」，
        导致 runner 拒绝推送。这里只回答「服务活着」。
        """
        if self.path.split("?")[0] == "/healthz":
            self._json({"ok": True, "service": "infotwitter-report"})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/api/feedback"):
            self._json({"ok": False, "error": "not found"}, 404)
            return

        try:
            raw_len = self.headers.get("Content-Length", "0")
            length = int(raw_len)
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "bad length"}, 400)
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._json({"ok": False, "error": "body too large"}, 413)
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "error": "bad request"}, 400)
            return

        story_id = str(payload.get("story_id") or "").strip()
        signal = str(payload.get("signal") or "").strip()
        token = str(payload.get("token") or "").strip()
        today = datetime.now(CST).date().isoformat()
        if not story_id or signal not in SIGNAL_INTEREST:
            self._json({"ok": False, "error": "story_id/signal 无效"}, 400)
            return
        if not token_ok(token):
            self._json({"ok": False, "error": "token 无效（页面已过期，请刷新日报页）"}, 403)
            return

        try:
            result = record_feedback(self.db_path, story_id, signal, payload.get("note"), today)
        except Exception:  # noqa: BLE001
            log.exception("反馈写入失败")
            self._json({"ok": False, "error": "internal"}, 500)
            return

        self._json({"ok": True, **result})


def token_ok(token: str, now: datetime | None = None) -> bool:
    """当天或前一天的令牌都接受。

    日报是每天 12:00 生成的静态页。只认"当天"的话，次日 00:00–12:00 之间
    页面上所有反馈按钮都会 403——而提示里写的"请刷新日报页"对静态文件毫无作用（D-04）。
    """
    base = (now or datetime.now(CST)).date()
    return any(
        verify_feedback_token((base - timedelta(days=i)).isoformat(), token)
        for i in (0, 1)
    )


def record_feedback(
    db_path: Path, story_id: str, signal: str, note: str | None = None, day: str | None = None
) -> dict[str, Any]:
    from .db import Database

    day = day or datetime.now(CST).date().isoformat()
    # 请求路径只拿连接，不跑建表/迁移：迁移在服务启动时做过一次就够了（D-05）
    db = Database(db_path, migrate=False)
    try:
        row = db.conn.execute(
            "SELECT category, event_type, entities, source_ids FROM stories WHERE id=?",
            (story_id,),
        ).fetchone()
        if row is None:
            # 不接受不存在的 story，避免孤儿反馈污染兴趣向量
            return {"story_id": story_id, "signal": signal, "ignored": "unknown story"}

        push = db.conn.execute(
            "SELECT id FROM pushes WHERE story_id=? ORDER BY id DESC LIMIT 1", (story_id,)
        ).fetchone()
        push_id = push["id"] if push else None

        db.conn.execute(
            "INSERT INTO feedback (push_id, story_id, signal, note, created_at) "
            "VALUES (?,?,?,?,?)",
            (push_id, story_id, signal, note, now_iso()),
        )
        db.conn.commit()

        duplicated = not db.claim_feedback_once(story_id, signal, day)
        if duplicated:
            return {"story_id": story_id, "signal": signal, "deduped": True}

        # 「假的」→ 下调来源层级；「重复」→ 只记错例，不动兴趣
        if signal == "fake":
            try:
                source_ids = json.loads(row["source_ids"] or "[]")
            except json.JSONDecodeError:
                source_ids = []
            for sid in source_ids[:3]:
                db.bump_fake_report(str(sid))
            return {"story_id": story_id, "signal": signal, "sources": source_ids[:3]}
        if signal == "duplicate":
            return {"story_id": story_id, "signal": signal, "logged": "duplicate"}

        delta = SIGNAL_INTEREST[signal]
        bumped: list[str] = []
        if delta:
            keys = [f"category:{row['category']}", f"type:{row['event_type']}"]
            try:
                entities = json.loads(row["entities"] or "[]")
            except json.JSONDecodeError:
                entities = []
            keys += [f"entity:{str(e).lower()}" for e in entities[:3]]
            for key in keys:
                db.bump_interest(key, delta)
                bumped.append(key)
        return {"story_id": story_id, "signal": signal, "interests": bumped}
    finally:
        db.close()


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def service_ready(port: int) -> bool:
    """端口通不代表是我们的服务：可能有别的程序占着 8080。

    优先用 /healthz 探活，而不是拿 /latest.html 当探针：
    latest.html 是**内容文件**，历史清空、首次部署、归档之后它本来就可能不存在，
    用它探活会把「服务好好的、只是今天还没发布」误判成「服务没起」，
    进而让 runner 拒绝推送（实测 2026-11-19 清空历史后 doctor 直接 FAIL）。
    老版本服务没有 /healthz，404 时再退回 latest.html 兜底。
    """
    if not port_open(port):
        return False
    import httpx

    for path in ("/healthz", "/latest.html"):
        try:
            resp = httpx.get(
                f"http://127.0.0.1:{port}{path}",
                timeout=4.0,
                follow_redirects=True,
                trust_env=False,
            )
        except Exception:  # noqa: BLE001
            continue
        if resp.status_code == 200:
            return True
    return False


def ensure_running(
    db_path: Path, report_dir: Path, port: int, wait: float = 6.0, host: str = "0.0.0.0"
) -> bool:
    """日报服务没在跑就拉起来。

    推送里的「完整版」链接依赖它。启动项只在登录时拉一次，
    服务白天崩掉的话，当天推送出去的就是死链——这里做自愈。
    """
    if service_ready(port):
        return True
    if port_open(port):
        # 端口被占用但不是我们的服务：再拉一个只会更乱
        log.warning("端口 %d 已被其他服务占用，未自动拉起日报服务", port)
        return False

    import subprocess
    import sys
    import time

    root = Path(__file__).resolve().parents[2]
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    exe = str(pythonw) if pythonw.exists() else sys.executable
    creation = 0
    if sys.platform == "win32":
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    log_path = root / "logs" / "service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as stream:
            subprocess.Popen(
                [exe, str(root / "run.py"), "serve", "--port", str(port), "--host", host],
                cwd=str(root),
                creationflags=creation,
                stdout=stream,
                stderr=stream,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("拉起日报服务失败：%s", exc)
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        if service_ready(port):
            log.info("日报服务已自动拉起（端口 %d）", port)
            return True
        time.sleep(0.5)
    log.warning("日报服务拉起后仍未就绪（端口 %d）", port)
    return False


def serve(db_path: Path, report_dir: Path, port: int, host: str = "0.0.0.0") -> None:
    class BoundHandler(Handler):
        pass

    BoundHandler.db_path = db_path
    BoundHandler.report_dir = report_dir
    handler = functools.partial(BoundHandler, directory=str(report_dir))

    with http.server.ThreadingHTTPServer((host, port), handler) as httpd:
        from .config import get_lan_ip

        lan = get_lan_ip()
        print(f"日报服务已启动：http://127.0.0.1:{port}/latest.html")
        if lan and host == "0.0.0.0":
            print(f"局域网地址：http://{lan}:{port}/latest.html")
        print(f"反馈接口：POST http://127.0.0.1:{port}/api/feedback（需页面内令牌）")
        print("Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
