"""环境自检：一眼看出「为什么今天没收到日报」。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .config import Config, ROOT
from .db import now_iso
from .db import Database

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _check_config(cfg: Config) -> Check:
    n = len(cfg.active_sources())
    if n == 0:
        return Check("配置 / 源", FAIL, "sources.yaml 中没有启用的源")
    return Check("配置 / 源", OK, f"{n} 个启用源，rubric={cfg.rubric_version}")


def _check_paths(cfg: Config) -> Check:
    problems = []
    for label, path in (("data", cfg.db_path.parent), ("reports", cfg.report_dir), ("logs", cfg.log_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{label}: {exc}")
    if problems:
        return Check("目录可写", FAIL, "；".join(problems))
    return Check(
        "目录可写", OK,
        f"db={cfg.db_path.parent.name}/ reports={cfg.report_dir.name}/ logs={cfg.log_dir.name}/（{ROOT}）",
    )


def _check_proxy(cfg: Config, runner: Callable[..., Any] | None = None) -> Check:
    if not cfg.proxy:
        return Check("网络 / 代理", WARN, "未探测到系统代理，境外源可能不可达")
    try:
        import httpx
    except ImportError:
        return Check("网络 / 代理", WARN, "httpx 未安装")

    # 用轻量端点探测，避免把 RSS 接口打出 429（那会误判成代理故障）
    url = "https://www.google.com/generate_204"
    last_err: Exception | None = None
    for _ in range(3):
        try:
            with httpx.Client(proxy=cfg.proxy, timeout=httpx.Timeout(12.0, connect=6.0)) as client:
                resp = client.get(url, headers={"User-Agent": cfg.user_agent})
            if resp.status_code < 400:
                return Check("网络 / 代理", OK, f"{cfg.proxy} 可达（HTTP {resp.status_code}）")
            return Check("网络 / 代理", WARN, f"{cfg.proxy} 返回 HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            import time

            time.sleep(1.5)
    return Check(
        "网络 / 代理", FAIL,
        f"{cfg.proxy} 连续 3 次不可用：{type(last_err).__name__}: {str(last_err)[:60]}",
    )


def _check_llm(cfg: Config) -> Check:
    provider = cfg.llm_provider
    if provider in ("none", "", "off"):
        return Check("LLM", WARN, "provider=none，将使用规则模式（无翻译/摘要）")
    if provider == "mock":
        return Check("LLM", WARN, "provider=mock，输出为模拟数据，勿用于正式推送")
    if provider == "deepseek":
        if cfg.llm_api_key:
            return Check("LLM", OK, f"deepseek / {cfg.llm_model}，key 已配置")
        return Check("LLM", FAIL, "缺少 DEEPSEEK_API_KEY，将降级为规则模式")
    if provider in ("ollama", "openai", "qwen"):
        # 兼容 OpenAI 协议的本地/第三方推理端点，只要有 base_url 就能跑
        return Check(
            "LLM", WARN,
            f"{provider} / {cfg.llm_model} @ {cfg.llm_base_url} · 连通性未验证",
        )
    return Check("LLM", WARN, f"未知 provider: {provider}")


def _check_push(cfg: Config, probe: bool = False) -> Check:
    channel = str(cfg.get("push.channel", "pushplus"))
    import os

    if cfg.get("push.dry_run"):
        # 正式配置里开着 dry_run，等于每天"成功"地什么都不发（D-08）
        return Check(
            "推送", FAIL,
            "push.dry_run=true —— 正式运行下这等于不推送，请改为 false",
        )

    if channel == "pushplus":
        token = str(cfg.get("push.pushplus_token", "") or "").strip() or os.environ.get(
            "PUSHPLUS_TOKEN", ""
        ).strip()
        if not token:
            return Check("推送", FAIL, "缺少 PushPlus token（.env 的 PUSHPLUS_TOKEN），日报只会打印到控制台")
        if len(token) < 16:
            return Check("推送", WARN, f"PushPlus token 长度异常（{len(token)}），请核对")
        if probe:
            return _probe_push(cfg)
        return Check(
            "推送", WARN,
            f"PushPlus token 已配置（长度 {len(token)}）· 模板 {cfg.get('push.pushplus_template', 'txt')}"
            " · 有效性未验证（加 --probe-push 会真发一条）",
        )

    if channel == "wecom_bot":
        webhook = str(cfg.get("push.wecom_webhook", "") or "").strip() or os.environ.get(
            "WECOM_WEBHOOK", ""
        ).strip()
        if not webhook:
            return Check("推送", FAIL, "缺少企业微信 webhook，日报只会打印到控制台")
        if not webhook.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="):
            return Check("推送", WARN, "webhook 格式不像企业微信群机器人地址")
        # 仅凭格式无法判定 key 是否有效：无效 key 也长得一样，只有真发才发现。
        return Check("推送", WARN, "企业微信 webhook 已配置，但有效性未验证（建议改用 PushPlus）")

    return Check("推送", WARN, f"渠道 {channel} 未实现，将退回控制台")


def _check_push_state(cfg: Config) -> Check:
    """推送状态机与站点的一致性检查。

    三项都是「静默失败」的高发点，光看定时任务 LastResult=0 发现不了：
    - 有卡在在途(2)的行：下次运行会收敛成失败并重推，但要让人知道发生过；
    - 很久没有成功投递：任务在跑但推送一直失败；
    - latest.html 不存在：读者点开推送里的「完整版」会 404。
    """
    if not cfg.db_path.exists():
        return Check("推送状态", WARN, "尚无数据库")
    try:
        db = Database(cfg.db_path, read_only=True)
    except sqlite3.Error as exc:
        return Check("推送状态", WARN, f"打不开数据库：{exc}")
    try:
        inflight = db.conn.execute(
            "SELECT COUNT(*) c FROM pushes WHERE delivery_ok=2").fetchone()["c"]
        last_ok = db.conn.execute(
            "SELECT MAX(push_date) d FROM pushes WHERE delivery_ok=1").fetchone()["d"]
        try:
            pending = db.conn.execute(
                "SELECT COUNT(*) c FROM pushes WHERE delivery_ok=0").fetchone()["c"]
        except sqlite3.Error:
            pending = 0
    finally:
        db.close()

    latest = cfg.report_dir / "latest.html"
    notes: list[str] = []
    level = OK
    if inflight:
        level = WARN
        notes.append(f"{inflight} 条在途未确认（下次运行会收敛成失败并重投）")
    if pending:
        notes.append(f"{pending} 条历史失败记录")
    if not last_ok:
        notes.append("还没有成功投递记录")
    else:
        try:
            days = (datetime.now(CST).date() - date.fromisoformat(str(last_ok)[:10])).days
            if days >= 2:
                level = WARN
                notes.append(f"最近一次成功投递在 {last_ok}（{days} 天前）")
            else:
                notes.append(f"最近成功投递 {last_ok}")
        except ValueError:
            notes.append(f"最近成功投递 {last_ok}")
    if not latest.exists():
        level = WARN if level == OK else level
        notes.append("latest.html 不存在（下次推送会生成，期间的「完整版」链接会 404）")
    return Check("推送状态", level, " · ".join(notes))


def _check_db(cfg: Config) -> Check:
    if not cfg.db_path.exists():
        return Check("数据库", WARN, f"尚未创建：{cfg.db_path}")
    try:
        db = Database(cfg.db_path)
        stats = {}
        for table in ("raw_docs", "stories", "pushes", "claims", "products", "price_points"):
            try:
                stats[table] = db.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            except sqlite3.Error:
                stats[table] = -1
        last = db.conn.execute("SELECT status, started_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        # 遗留表 skus / price_snapshots 已于 2026-11-19 从 schema 移除，这里不再检查
        db.close()
    except sqlite3.Error as exc:
        return Check("数据库", FAIL, str(exc))

    detail = " / ".join(f"{k}={v}" for k, v in stats.items())
    if last:
        detail += f" · 上次运行 {last['status']} @ {last['started_at'][:16]}"
    return Check("数据库", OK, detail)


def _check_sources(cfg: Config) -> Check:
    if not cfg.db_path.exists():
        return Check("源健康度", WARN, "尚无采集记录")
    try:
        db = Database(cfg.db_path)
        rows = db.conn.execute(
            "SELECT fail_count, name FROM sources WHERE active=1 ORDER BY fail_count DESC LIMIT 5"
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return Check("源健康度", WARN, "读取失败")

    if not rows:
        return Check("源健康度", WARN, "无记录")
    bad = [r["name"] for r in rows if r["fail_count"] >= 3]
    if bad:
        return Check("源健康度", WARN, f"连续失败≥3：{', '.join(bad[:5])}")
    worst = rows[0]
    return Check("源健康度", OK, f"最高失败计数 {worst['fail_count']}（{worst['name']}）")


def _check_task(task_name: str = "InfoTwitter-Daily") -> Check:
    """报告任务真实状态：启用？上次结果？下次时间？

    只看「已注册」会让被禁用、连续失败、python 路径失效的任务都显示 OK（BUG-14）。
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return Check("定时任务", WARN, "非 Windows，跳过")

    ps = (
        f"$t=Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue;"
        "if(-not $t){exit 9};"
        "$i=Get-ScheduledTaskInfo -TaskName "
        f"'{task_name}';"
        "'{0}|{1}|{2}' -f $t.State,$i.LastTaskResult,"
        "$(if($i.NextRunTime){$i.NextRunTime.ToString('yyyy-MM-dd HH:mm')}else{'-'})"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return Check("定时任务", WARN, f"查询失败：{exc}")
    if out.returncode == 9:
        return Check("定时任务", WARN, f"未注册 {task_name}")
    if out.returncode != 0:
        return Check("定时任务", WARN, f"查询失败：{(out.stderr or '').strip()[:80]}")

    parts = (out.stdout or "").strip().split("|")
    if len(parts) != 3:
        return Check("定时任务", WARN, f"无法解析任务状态：{(out.stdout or '').strip()[:60]}")
    state, last_result, next_run = parts

    if state.lower() == "running":
        # 任务正在执行（比如 doctor 就是从任务里跑的），这是正常状态
        return Check("定时任务", WARN, f"{task_name} 正在运行中 · 下次 {next_run}")
    if state.lower() not in ("ready", "queued"):
        return Check("定时任务", FAIL, f"{task_name} 状态 {state}（应为 Ready），本次不会执行")
    try:
        code = int(last_result)
    except ValueError:
        code = -1
    # 任务计划程序自己的状态码，不是失败：
    #   0x41303 (267011) 从未运行过（刚注册完就是这个）
    #   0x41301 (267009) 正在运行
    if code in (267011, 0x41303):
        return Check("定时任务", WARN, f"{task_name} 已注册但尚未运行过 · 下次 {next_run}")
    if code in (267009, 0x41301):
        return Check("定时任务", WARN, f"{task_name} 正在运行中 · 下次 {next_run}")
    if code != 0:
        return Check(
            "定时任务", FAIL,
            f"{task_name} 上次执行失败（LastTaskResult=0x{code & 0xFFFFFFFF:X}），"
            "详见 logs/run-*.log 与 logs/alert.log",
        )
    return Check("定时任务", OK, f"已注册且上次成功 · 下次 {next_run}")


def _check_report_url(cfg: Config) -> Check:
    """推送里的「完整版」链接能不能打开，取决于这里。

    必须按推送实际使用的路径做真实 HTTP GET：只探端口 + 只验 latest.html
    会让 /reports/ 死链永远通过（核查报告 BUG-01 / BUG-14）。
    """
    import datetime as _dt

    from .config import get_lan_ip

    base = cfg.report_base_url()
    lan = get_lan_ip()
    today = _dt.datetime.now().date().isoformat()
    target = f"{base}/{today}.html"
    if not lan:
        return Check("日报页", WARN, f"{base}（未取到局域网 IP，手机可能打不开）")

    import httpx

    def _get(url: str):
        try:
            return httpx.get(url, timeout=6.0, follow_redirects=True, trust_env=False)
        except Exception:  # noqa: BLE001
            return None

    resp = _get(target)
    if resp is None:
        # 连不上：服务没起，或端口被别的程序占着
        latest = _get(f"{base}/latest.html")
        if latest is not None and latest.status_code == 200:
            return Check(
                "日报页", WARN,
                f"{target} 连不上但 latest.html 可访问，检查 report_host / 端口占用",
            )
        return Check(
            "日报页", WARN,
            f"{base} 连不上；用 run.py serve 或注册 serve 启动项",
        )

    if resp.status_code == 200 and len(resp.content) >= 500:
        return Check("日报页", OK, f"{target} 可访问（{len(resp.content)} 字节）")
    if resp.status_code == 200:
        return Check("日报页", WARN, f"{target} 只有 {len(resp.content)} 字节，疑似空页")
    if resp.status_code >= 500:
        return Check("日报页", FAIL, f"{target} 返回 HTTP {resp.status_code}，服务异常")

    # 404：当天日报还没生成（12:00 的任务才写它），这不是死链——
    # 之前这里直接判 FAIL，导致每天上午必然假红（核查报告 A-07）
    latest = _get(f"{base}/latest.html")
    if latest is not None and latest.status_code == 200:
        return Check(
            "日报页", WARN,
            f"今日日报尚未生成（12:00 任务后才有 {today}.html）；latest.html 可访问（{len(latest.content)} 字节）",
        )
    return Check(
        "日报页", FAIL,
        f"{target} 与 latest.html 都不可用（HTTP {resp.status_code}）—— 推送里的链接会打不开",
    )


def _probe_push(cfg: Config) -> Check:
    """真发一条测试推送：token 有没有效只有发出去才知道（D-08）。"""
    from .notify import build_notifier

    try:
        notifier = build_notifier(cfg, force_preview=False)
        ok, msg = notifier.send(
            "InfoTwitter 自检",
            f"{now_iso()} 这是一条 doctor --probe-push 的测试消息，收到即说明通道可用。",
        )
    except Exception as exc:  # noqa: BLE001
        return Check("推送", FAIL, f"发送测试消息抛异常：{type(exc).__name__}: {exc}")
    if ok:
        return Check("推送", OK, f"测试消息已发送（{notifier.name}）：{msg}")
    return Check("推送", FAIL, f"测试消息发送失败（{notifier.name}）：{msg}")


def run_checks(cfg: Config, probe_push: bool = False) -> list[Check]:
    return [
        _check_config(cfg),
        _check_paths(cfg),
        _check_proxy(cfg),
        _check_llm(cfg),
        _check_push(cfg, probe=probe_push),
        _check_report_url(cfg),
        _check_db(cfg),
        _check_push_state(cfg),
        _check_sources(cfg),
        _check_task(str(cfg.get("app.task_name", "InfoTwitter-Daily"))),
        # 每周新品任务没注册不影响日更，所以缺了只提示不报错
        _check_task(str(cfg.get("pricing.weekly_task_name", "InfoTwitter-Price-Weekly"))),
    ]


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks) + 2
    lines = []
    for c in checks:
        lines.append(f"[{c.status}] {c.name:<{width}} {c.detail}")
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    if fails:
        lines.append(f"{fails} 项失败 / {warns} 项警告 —— 日报可能推送不出来")
    elif warns:
        lines.append(f"{warns} 项警告，功能可用但效果会打折")
    else:
        lines.append("全部通过")
    return "\n".join(lines)
