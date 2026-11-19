"""NGA 适配器：登录态 + HTML 解析（NGA 不支持 json=1，返回的是 GBK 页面）。

关键点：
- 必须带登录 cookie，否则整站 403
- 响应是 GBK 编码，且 `&json=1` 不生效，只能解析 HTML
- 版块页的帖子行是 `.topicrow`，里面有 `read.php?tid=` 链接和 10 位 unix 时间戳
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import CST, now_iso

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
COOKIE_PATH = ROOT / "data" / "cookies_nga.json"
PROFILE_DIR = ROOT / "data" / "browser_profile" / "nga"
HOME = "https://ngabbs.com/"
LOGIN_COOKIES = ("ngaPassportUid", "ngaPassportCid")

_TID_RE = re.compile(r"tid=(\d+)")
_TS_RE = re.compile(r"\b(1\d{9})\b")


def load_cookies() -> dict[str, str]:
    if not COOKIE_PATH.exists():
        return {}
    try:
        raw = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, list):
        return {str(c["name"]): str(c["value"]) for c in raw if c.get("name")}
    return {str(k): str(v) for k, v in raw.items()}


def _has_login(cookies: dict[str, str]) -> bool:
    return all(k in cookies for k in LOGIN_COOKIES)


def login(wait_seconds: int = 300, proxy: str | None = None, user_agent: str = "") -> bool:
    """打开可见浏览器让用户登录，成功后把 cookie 落盘。"""
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n即将打开浏览器，请登录 NGA（扫码或账号密码）。最多等待 {wait_seconds} 秒。\n", flush=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            proxy={"server": proxy} if proxy else None,
            user_agent=user_agent or None,
            locale="zh-CN",
            viewport={"width": 1440, "height": 1000},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            import time as _t

            deadline = _t.time() + wait_seconds
            last = 0
            while _t.time() < deadline:
                page.wait_for_timeout(2500)
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                if _has_login(cookies):
                    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    COOKIE_PATH.write_text(
                        json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    print(f"✅ NGA 登录成功，cookie 已保存（{len(cookies)} 条）", flush=True)
                    return True
                elapsed = int(_t.time() - (deadline - wait_seconds))
                if elapsed - last >= 20:
                    last = elapsed
                    print(f"   [{elapsed:>3}s] 等待登录…", flush=True)
            print("❌ 超时：未检测到 NGA 登录 cookie", flush=True)
            return False
        finally:
            ctx.close()


def _get_html(url: str, cookies: dict[str, str], proxy: str | None, user_agent: str) -> str | None:
    import httpx

    headers = {
        "User-Agent": user_agent or "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Referer": HOME,
    }
    try:
        with httpx.Client(proxy=proxy, timeout=25.0, follow_redirects=True,
                          cookies=cookies, headers=headers) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            log.warning("NGA %s 返回 HTTP %s", url[:60], resp.status_code)
            return None
        return resp.content.decode("gbk", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("NGA 请求失败 %s: %s", url[:60], str(exc)[:70])
        return None


def fetch_post_body(
    tid: str, cookies: dict[str, str], proxy: str | None, user_agent: str, max_chars: int = 4000
) -> str:
    """抓帖子首楼正文（版块列表只给标题，正文要单独取一次）。"""
    from bs4 import BeautifulSoup

    html = _get_html(f"https://ngabbs.com/read.php?tid={tid}", cookies, proxy, user_agent)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#postcontent0") or soup.select_one(".postcontent")
    if not node:
        return ""
    text = node.get_text(" ", strip=True)
    return text[:max_chars] if text else ""


def parse_board(html: str) -> list[dict[str, Any]]:
    """从版块页解析帖子：tid、标题、最后回复时间戳。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for row in soup.select(".topicrow"):
        a = row.select_one("a.topic") or row.select_one('a[href*="read.php?tid="]')
        if not a or not a.get("href"):
            continue
        m = _TID_RE.search(a["href"])
        if not m:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        # 行内嵌着 10 位 unix 时间戳（最后回复时间）
        ts = _TS_RE.search(row.get_text(" ", strip=True))
        out.append({
            "tid": m.group(1),
            "title": title,
            "lastpost": int(ts.group(1)) if ts else None,
        })
    return out


def to_docs(source: dict[str, Any], threads: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in threads:
        published: datetime | None = None
        if t.get("lastpost"):
            try:
                published = datetime.fromtimestamp(int(t["lastpost"]), tz=CST)
            except (TypeError, ValueError, OSError):
                published = None
        if published is not None and published < since:
            continue
        out.append({
            "source_id": source["id"],
            "url": f"https://ngabbs.com/read.php?tid={t['tid']}",
            "title": t["title"],
            "body": f"{t['title']}（NGA·{source.get('board') or source['id']}）",
            "lang": "zh",
            "published_at": published.replace(microsecond=0).isoformat() if published else None,
            "fetched_at": now_iso(),
            "content_hash": _hash(t["title"], t["tid"]),
            "publisher_domain": "ngabbs.com",
            "time_source": "article" if published else "fetch",
            "engagement": 0,
        })
    return out


def _hash(title: str, tid: str) -> str:
    import hashlib

    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha1(f"{norm}|{tid}".encode("utf-8")).hexdigest()


class NgaAdapter:
    def __init__(self, proxy: str | None = None, user_agent: str = "") -> None:
        self.proxy = proxy
        self.user_agent = user_agent

    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]] | None:
        fid = str(source.get("fid") or "").strip()
        if not fid:
            log.warning("源 %s 缺少 fid", source.get("id"))
            return None
        cookies = load_cookies()
        if not _has_login(cookies):
            log.warning("NGA 未登录，跳过 %s（先运行 run.py login nga）", source.get("id"))
            return None

        pages = max(1, int(source.get("pages", 2) or 2))
        threads: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pn in range(1, pages + 1):
            url = f"https://ngabbs.com/thread.php?fid={fid}" + ("" if pn == 1 else f"&page={pn}")
            html = _get_html(url, cookies, self.proxy, self.user_agent)
            if html is None:
                return None if pn == 1 else threads and to_docs(source, threads, since)
            batch = parse_board(html)
            added = 0
            for t in batch:
                if t["tid"] in seen:
                    continue
                seen.add(t["tid"])
                threads.append(t)
                added += 1
            if added == 0:
                break

        docs = to_docs(source, threads, since)

        # 补首楼正文：列表页只有标题，正文要逐帖再取一次（默认只补前 5 条）
        body_lookup = int(source.get("body_lookup", 5) or 0)
        filled = 0
        for doc in docs[:body_lookup]:
            tid = doc["url"].rsplit("tid=", 1)[-1]
            text = fetch_post_body(tid, cookies, self.proxy, self.user_agent)
            if text and len(text) > len(doc.get("body") or ""):
                doc["body"] = text
                filled += 1
        log.info(
            "NGA %s（fid=%s）：%d 帖，窗口内 %d 条，补正文 %d 条",
            source.get("board") or source["id"], fid, len(threads), len(docs), filled,
        )
        return docs
