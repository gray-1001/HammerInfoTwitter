"""M2 系统改造：HTTP 优先 + 浏览器兜底。

改造前的两个硬伤：
1. 只有 Playwright 一条路，单篇约 12 秒，263 篇要 52 分钟 → 只能给 6 个源开 deep_fetch；
2. 35 个源的文档从来没有正文，摘要全靠 RSS 一句话。

实测（2026-11-18）：
    wargamer            0.5s / 2,479 字
    goonhammer          1.2s / 3,540 字
    bilibili（4 个源）   0.5~0.8s / 1,000~1,900 字   ← 原来 body 只有 1~258 字
    whc_news            0.6s / 3,256 字（偶发 ConnectError，需回落）
    spikeybits          403（必须回落浏览器）
    youtube / reddit    145 字样board / 0 字（不值得抓）

改造后单篇约 1 秒，浏览器只在 HTTP 失败时才启动（惰性），
全量抓取从「不可行」变成「几分钟」。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import trafilatura

log = logging.getLogger(__name__)

CHALLENGE_MARKERS = (
    "just a moment",
    "security verification",
    "checking your browser",
    "enable javascript and cookies",
    "百度安全验证",
    "performing security verification",
    "attention required",
    "sign in to continue",
    "log in to continue",
    "subscribe to read",
    "subscribers only",
    "this content is for members",
    "登录后查看",
    "请先登录",
    "该内容需要订阅",
    "region is not available",
)

MIN_CHARS = 250  # 短于此视为抽取失败
# 原来是 400，但 Warhammer Community 的 Rumour Engine 这类帖子正文只有 300 字左右，
# 卡在 400 会被判成失败 → 白跑一次浏览器（实测 3 篇各花 12 秒最后仍回写 0 条）。
# 降到 250 后要防「把导航/列表当正文」，因此再要求至少两个句末标点。
_SENTENCE_END = re.compile(r"[.!?。！？]")


def looks_like_prose(text: str) -> bool:
    """粗判是否成句正文：导航/列表通常没有句末标点。"""
    body = (text or "").strip()
    if len(body) < MIN_CHARS:
        return False
    return len(_SENTENCE_END.findall(body)) >= 2

# 抓了也没用的域名：Google News 跳转页是纯 JS 壳，YouTube/Reddit 是反爬或导航
# Google News 的条目链接是 JS 壳，httpx 跟不进去，但浏览器能落到真实文章页
GOOGLE_NEWS_HOST = "news.google.com"

USELESS_HOSTS = frozenset({
    "news.google.com",
    "www.youtube.com",
    "youtube.com",
    "youtu.be",
    "www.reddit.com",
    "reddit.com",
    "old.reddit.com",
})

_DATE_PUBLISHED_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')

# HTTP 响应里明显是反爬页的特征（配合 status 一起判断）
_HTTP_BLOCK_STATUS = frozenset({401, 403, 405, 406, 429, 451, 503})


def extract_date_published(html: str) -> str:
    """从文章页 HTML 里取 datePublished，返回 CST 的 ISO 时间；取不到返回空串。"""
    from datetime import datetime, timezone

    m = _DATE_PUBLISHED_RE.search(html or "")
    if not m:
        return ""
    raw = m.group(1).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    from .db import CST

    return dt.astimezone(CST).replace(microsecond=0).isoformat()


def extract_text(html: str) -> str:
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001
        return ""
    return (text or "").strip()


def looks_blocked(text: str) -> bool:
    low = (text or "")[:2000].lower()
    return any(marker in low for marker in CHALLENGE_MARKERS)


def is_useless_url(url: str) -> bool:
    """跳转页/视频站/论坛，抓了也拿不到正文。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return True
    return host in USELESS_HOSTS


def _http_client(cfg: Any):
    """造一个带代理与超时的 httpx 客户端。"""
    import httpx

    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9,zh-CN;q=0.8",
    }
    timeout = float(cfg.get("content.http_timeout", 15) or 15)
    return httpx.Client(
        proxy=cfg.proxy,
        timeout=httpx.Timeout(timeout, connect=min(8.0, timeout)),
        follow_redirects=True,
        headers=headers,
    )


def fetch_http_one(
    client: Any,
    url: str,
    retries: int = 2,
    deadline_at: float = 0.0,
    dates: dict[str, str] | None = None,
) -> tuple[str, str]:
    """HTTP 直连抓一篇。

    返回 (正文, 原因)。原因取值：
      ""      成功
      "skip"  跳转页/视频站，不该抓
      "block" 反爬/登录墙，应交浏览器兜底
      "short" 正文过短，应交浏览器兜底
      "error" 网络错误
    """
    if is_useless_url(url):
        return "", "skip"

    for attempt in range(1, retries + 1):
        if deadline_at and time.time() > deadline_at:
            return "", "error"
        try:
            resp = client.get(url)
            if resp.status_code in _HTTP_BLOCK_STATUS:
                log.debug("HTTP %d，交浏览器：%s", resp.status_code, url[:70])
                return "", "block"
            if resp.status_code >= 400:
                return "", "error"
            html = resp.text
            if dates is not None:
                published = extract_date_published(html)
                if published:
                    dates[url] = published
            text = extract_text(html)
            if looks_blocked(text):
                return "", "block"
            if not looks_like_prose(text):
                return "", "short"
            return text, ""
        except Exception as exc:  # noqa: BLE001
            log.debug("HTTP 抓取失败（第 %d 次）%s: %s", attempt, url[:60], str(exc)[:60])
            if attempt < retries and not (deadline_at and time.time() > deadline_at):
                time.sleep(0.5 * attempt)
    return "", "error"


def fetch_fulltext(
    items: list[dict[str, Any]],
    proxy: str | None,
    user_agent: str,
    wait_ms: int = 2500,
    retry_wait_ms: int = 6000,
    max_items: int = 40,
    deadline_sec: float = 0,
    dates: dict[str, str] | None = None,
    cfg: Any = None,
    resolved: dict[str, str] | None = None,
) -> dict[str, str]:
    """两段式抓取：先批量走 HTTP，失败的再交给浏览器。"""
    if not items:
        return {}

    targets = items[:max_items]
    t0 = time.time()
    deadline_at = (t0 + deadline_sec) if deadline_sec else 0.0
    results: dict[str, str] = {}
    # 单篇上限：实测某个合集页能抽出 9 万字（目录+评论），
    # LLM 只读前 8000 字，多存的只是噪音与磁盘占用
    max_chars = 12000 if cfg is None else int(cfg.get("content.max_chars", 12000) or 0)

    http_ok = browser_ok = skipped = 0
    need_browser: list[dict[str, Any]] = []
    # 同一域名连续失败到阈值后，本轮不再尝试 HTTP：
    # whc_news 这类站点 HTTP 会先超时 8 秒再回落浏览器，比直接用浏览器还慢
    dead_hosts: dict[str, int] = {}
    dead_threshold = 2 if cfg is None else int(cfg.get("content.http_dead_threshold", 2) or 2)

    # ---------- 第一段：HTTP ----------
    http_first = True if cfg is None else bool(cfg.get("content.http_first", True))
    if http_first:
        retries = 2 if cfg is None else int(cfg.get("content.http_retries", 2) or 2)
        client = _http_client(cfg) if cfg is not None else None
        try:
            for item in targets:
                if deadline_sec and time.time() - t0 > deadline_sec:
                    log.warning("HTTP 段超时，剩余交给浏览器或跳过")
                    break
                url = item["url"]
                host = (urlparse(url).hostname or "").lower()

                if host == GOOGLE_NEWS_HOST:
                    # 交给浏览器还原，不参与 HTTP 计数与熔断
                    need_browser.append(item)
                    continue

                if dead_hosts.get(host, 0) >= dead_threshold:
                    need_browser.append(item)
                    continue

                text, why = fetch_http_one(
                    client, url, retries=retries, deadline_at=deadline_at, dates=dates
                ) if client is not None else ("", "error")
                if text:
                    results[url] = text[:max_chars] if max_chars else text
                    http_ok += 1
                    dead_hosts[host] = 0
                    log.debug("HTTP 正文 %d 字 %s", len(text), url[:70])
                elif why == "skip":
                    skipped += 1
                else:
                    if why in ("error", "block"):
                        dead_hosts[host] = dead_hosts.get(host, 0) + 1
                        if dead_hosts[host] == dead_threshold:
                            log.info("域名 %s HTTP 连续失败，本轮改走浏览器", host)
                    need_browser.append(item)
        finally:
            if client is not None:
                client.close()
        log.info(
            "HTTP 段完成：成功 %d，待浏览器 %d，跳过 %d",
            http_ok, len(need_browser), skipped,
        )
    else:
        need_browser = list(targets)

    # ---------- 第二段：浏览器（惰性启动） ----------
    browser_enabled = True if cfg is None else bool(cfg.get("content.browser_fallback", True))
    if need_browser and browser_enabled:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                proxy={"server": proxy} if proxy else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                ctx = browser.new_context(
                    user_agent=user_agent,
                    viewport={"width": 1440, "height": 1000},
                    locale="en-GB",
                )
                page = ctx.new_page()
                for idx, item in enumerate(need_browser, 1):
                    if deadline_sec and time.time() - t0 > deadline_sec:
                        log.warning(
                            "正文抓取超时（%.0fs 预算），已抓 %d 篇，剩余跳过",
                            deadline_sec, len(results),
                        )
                        break
                    url = item["url"]
                    if (urlparse(url).hostname or "").lower() == GOOGLE_NEWS_HOST:
                        text = _resolve_and_fetch(
                            page, url, wait_ms, deadline_at=deadline_at,
                            dates=dates, resolved=resolved,
                        )
                    else:
                        text = _fetch_one(page, url, wait_ms, retry_wait_ms,
                                          deadline_at=deadline_at, dates=dates)
                    if text:
                        results[url] = text[:max_chars] if max_chars else text
                        browser_ok += 1
                        log.debug("浏览器正文 %d 字 %s", len(text), url[:70])
                    if idx % 10 == 0:
                        log.info("浏览器段进度 %d/%d", idx, len(need_browser))
            finally:
                browser.close()

    log.info(
        "正文抓取完成：%d/%d 成功（HTTP %d / 浏览器 %d / 跳过 %d），用时 %.1fs",
        len(results), len(targets), http_ok, browser_ok, skipped, time.time() - t0,
    )
    return results


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """sqlite3.Row 没有 .get，测试里也常传普通 dict，这里统一取值。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _url_hash(url: str) -> str:
    import hashlib

    return hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()


def _fetch_one(
    page: Any, url: str, wait_ms: int, retry_wait_ms: int, attempts: int = 2,
    deadline_at: float = 0.0, dates: dict[str, str] | None = None,
) -> str:
    """抓单页正文，网络错误按退避重试（BUG-24：正文抓取原先 0 次重试）。

    deadline_at 是绝对时间戳。单篇最坏要 90s（两次 goto 各 40s + 等待），
    只在每篇开始前检查预算会让一次运行超时很久（C-08）。

    dates 非空时，顺便把文章页 JSON-LD 的发布日期写进去——这是列表页
    取不到/取错日期时的兜底来源（P0-1）。
    """
    for attempt in range(1, attempts + 1):
        if deadline_at and time.time() > deadline_at:
            log.debug("已到抓取预算，放弃 %s", url[:60])
            return ""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            if dates is not None:
                published = extract_date_published(html)
                if published:
                    dates[url] = published
            text = extract_text(html)
            # Cloudflare 挑战页：等久一点再取一次
            if looks_blocked(text) or len(text) < MIN_CHARS:
                page.wait_for_timeout(retry_wait_ms)
                html = page.content()
                if dates is not None and url not in dates:
                    published = extract_date_published(html)
                    if published:
                        dates[url] = published
                text = extract_text(html)
            if looks_like_prose(text) and not looks_blocked(text):
                return text
            return ""
        except Exception as exc:  # noqa: BLE001
            log.debug("正文抓取失败（第 %d 次）%s: %s", attempt, url[:60], str(exc)[:60])
            if attempt < attempts and not (deadline_at and time.time() > deadline_at):
                time.sleep(1.5 * attempt)
    return ""


def cache_get(conn: Any, url: str, max_age_days: int = 0) -> str | None:
    """读正文缓存。max_age_days > 0 时过期视为未命中。

    以前只按 url_hash 取，不校验 fetched_at，过期内容要等 prune 才失效；
    而 prune 排在 enrich 之后，等于"过期后的第一次运行仍然复用旧抽取结果"（C-07）。
    """
    row = conn.execute(
        "SELECT text, fetched_at FROM content_cache WHERE url_hash=?", (_url_hash(url),)
    ).fetchone()
    if not row:
        return None
    if max_age_days and row["fetched_at"]:
        from .db import CST

        cutoff = (datetime.now(CST) - timedelta(days=max(1, max_age_days))).isoformat()
        if str(row["fetched_at"]) < cutoff:
            return None
    return row["text"]


def cache_put(conn: Any, url: str, text: str, max_chars: int = 0) -> None:
    """写正文缓存。max_chars > 0 时先截断——

    以前只截返回值不截缓存，历史遗留的 6.8 万字条目会一直被原样复用，
    DB 和缓存都跟着膨胀（LLM 只读前 8000 字）。
    """
    from .db import now_iso

    if max_chars:
        text = text[:max_chars]
    conn.execute(
        "INSERT OR REPLACE INTO content_cache (url_hash, url, text, char_count, fetched_at) "
        "VALUES (?,?,?,?,?)",
        (_url_hash(url), url, text, len(text), now_iso()),
    )


def backfill_dates(cfg: Any, db: Any, limit: int = 20, proxy: str | None = None) -> int:
    """给「没有发布日期」的浏览器源文档补日期（P0-1）。

    只补日期、不抓正文，所以很便宜：每篇一次导航。限额是为了不把日更拖长。
    缓存命中的文档不会走正文抓取，也就拿不到日期，必须靠这一趟。
    """
    rows = db.conn.execute(
        "SELECT r.id, r.url FROM raw_docs r JOIN sources s ON s.id = r.source_id"
        " WHERE r.published_at IS NULL AND (s.fetch_method = 'browser' OR s.fetch_method = 'rss')"
        "   AND COALESCE(r.discovered_at, r.fetched_at) >= ?"
        " ORDER BY r.id DESC LIMIT ?",
        ((datetime.now() - timedelta(days=30)).isoformat(), max(1, limit)),
    ).fetchall()
    if not rows:
        return 0

    from playwright.sync_api import sync_playwright

    fixed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(user_agent=cfg.user_agent, locale="en-GB")
            page = ctx.new_page()
            for r in rows:
                try:
                    page.goto(r["url"], wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(1200)
                    published = extract_date_published(page.content())
                except Exception as exc:  # noqa: BLE001
                    log.debug("补日期失败 %s: %s", r["url"][:60], str(exc)[:60])
                    continue
                if not published:
                    continue
                db.conn.execute(
                    "UPDATE raw_docs SET published_at=?, time_source='article' WHERE id=?",
                    (published, r["id"]),
                )
                fixed += 1
        finally:
            browser.close()
    if fixed:
        db.conn.commit()
        log.info("补回发布日期 %d 条", fixed)
    return fixed


def enrich_docs(cfg: Any, db: Any, docs: list[dict[str, Any]],
                stats: dict[str, Any] | None = None) -> int:
    """为文档补抓正文并回写。已抓过的 URL 走缓存，不重复请求。

    stats 传入时会把「请求数 / 成功 / 失败」等分类计数填进去：
    以前只返回成功条数，失败被静默吞掉，日志照样打印「完成」，
    于是正文缺失率 68% 这件事一直没人看见（C-03：失败要分类计数并告警）。
    """
    if not cfg.get("content.enabled", True):
        return 0

    deep_sources = {s["id"] for s in cfg.sources if s.get("deep_fetch")}
    # Google News 的链接本身抓不到内容，但浏览器能还原到真实文章页，
    # 因此开启还原时把 googlenews 源也纳入正文抓取范围
    if cfg.get("content.resolve_google_news", True):
        deep_sources |= {s["id"] for s in cfg.sources if s.get("type") == "googlenews"}
    if not deep_sources:
        return 0

    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    cached_hits = 0
    # 抓取优先级：L0/L1 先抓。以前 targets 的顺序就是 query 顺序（按发现时间倒序），
    # 一条刚抓到的 L2 水帖会排在几小时前的官方头条前面；max_items 截断时
    # 先丢的恰恰是官方一手内容。
    tier_rank = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
    ttl_days = int(cfg.get("retention.content_cache_days", 14) or 0)
    # Google News 还原每条约 3.5 秒，单独设预算，别把整轮的正文时间吃光
    gn_budget = int(cfg.get("content.resolve_limit", 30) or 0) if cfg.get(
        "content.resolve_google_news", True) else 0
    for r in docs:
        if r["source_id"] not in deep_sources or not r["url"]:
            continue
        host = (urlparse(r["url"]).hostname or "").lower()
        url = r["url"]
        if url in seen:
            continue
        seen.add(url)
        cached = cache_get(db.conn, url, max_age_days=ttl_days)
        # 额度只按「真要发请求的」扣：缓存命中的条目本来就不花时间，
        # 让它们吃掉 resolve_limit 会让预算坐不满（C-19）
        if host == GOOGLE_NEWS_HOST and not cached:
            if gn_budget <= 0:
                continue
            gn_budget -= 1
        if cached:
            cached_hits += 1
            targets.append({"url": url, "id": r["id"], "cached": cached,
                            "tier": tier_rank.get(str(_row_get(r, "tier", "L9")), 9)})
        else:
            targets.append({"url": url, "id": r["id"], "cached": None,
                            "tier": tier_rank.get(str(_row_get(r, "tier", "L9")), 9)})

    if not targets:
        return 0

    # 待抓列表按「层级优先」稳定排序：同级内保持原有顺序（发现时间倒序）
    to_fetch = sorted(
        (t for t in targets if not t["cached"]), key=lambda x: x.get("tier", 9)
    )
    limit = int(cfg.get("content.max_docs", 0) or 0)
    if limit and len(to_fetch) > limit:
        log.warning("正文抓取超上限：%d 篇只抓前 %d 篇", len(to_fetch), limit)
        to_fetch = to_fetch[:limit]

    # max_items 以前传的是 len(to_fetch)，等于没有上限（C-08）
    hard_cap = int(cfg.get("content.max_items", 60) or 0)
    fetch_cap = min(len(to_fetch), hard_cap) if hard_cap else len(to_fetch)

    log.info(
        "=== 正文抓取：待抓 %d 篇（上限 %d），缓存命中 %d 篇 ===",
        len(to_fetch), fetch_cap, cached_hits,
    )
    # 列表页取不到（或曾经取错）的发布日期，用文章页 JSON-LD 补上
    doc_dates: dict[str, str] = {}
    resolved_urls: dict[str, str] = {}
    fetched = fetch_fulltext(
        to_fetch,
        proxy=cfg.proxy,
        user_agent=cfg.user_agent,
        wait_ms=int(cfg.get("content.wait_ms", 2500)),
        max_items=max(1, fetch_cap),
        deadline_sec=float(cfg.get("content.deadline_sec", 1800)),
        dates=doc_dates,
        cfg=cfg,
        resolved=resolved_urls,
    )

    updated = 0
    date_fixed = 0
    url_fixed = 0
    with db.tx() as conn:
        for t in targets:
            # Google News 跳转被还原后，把库里的地址换成真实文章页：
            # 报告页的「原文链接」不再是 Google 中转页，去重也能按真域名算
            real = resolved_urls.get(t["url"])
            if real:
                conn.execute(
                    "UPDATE raw_docs SET url=?, publisher_domain=? WHERE id=?",
                    (real, (urlparse(real).hostname or "").removeprefix("www."), t["id"]),
                )
                url_fixed += 1
            text = t["cached"] or fetched.get(t["url"])
            if not text:
                continue
            if not t["cached"]:
                cache_put(conn, t["url"], text, max_chars=int(cfg.get("content.max_chars", 12000) or 0))
            current = conn.execute(
                "SELECT length(coalesce(body,'')) n FROM raw_docs WHERE id=?", (t["id"],)
            ).fetchone()
            # 只有明显更完整时才覆盖，避免用残页替换掉原摘要
            if current and len(text) > (current["n"] or 0) * 2:
                conn.execute("UPDATE raw_docs SET body=? WHERE id=?", (text, t["id"]))
                updated += 1

            # 用文章页的真实发布日期纠正列表页日期（P0-1）。
            # 只在"为空"或"日期不一致"时改，且日期相同就不动，避免每天无谓写库。
            published = doc_dates.get(t["url"])
            if published:
                row = conn.execute(
                    "SELECT published_at, time_source FROM raw_docs WHERE id=?", (t["id"],)
                ).fetchone()
                old = str(row["published_at"] or "")[:10] if row else ""
                if old != published[:10]:
                    conn.execute(
                        "UPDATE raw_docs SET published_at=?, time_source='article' WHERE id=?",
                        (published, t["id"]),
                    )
                    date_fixed += 1
    if url_fixed:
        log.info("还原 Google News 跳转 %d 条", url_fixed)
    if date_fixed:
        log.info("用文章页纠正发布日期 %d 条", date_fixed)
    log.info("正文回写 %d 条（缓存命中 %d）", updated, cached_hits)

    if stats is not None:
        requested = min(len(to_fetch), fetch_cap)
        got = sum(1 for t in to_fetch[:requested] if fetched.get(t["url"]))
        stats.update({
            "targets": len(targets),
            "cached": cached_hits,
            "requested": requested,
            "ok": got,
            "failed": max(0, requested - got),
            "updated": updated,
            "date_fixed": date_fixed,
            "url_fixed": url_fixed,
        })
    return updated


def _resolve_and_fetch(
    page: Any,
    url: str,
    wait_ms: int,
    deadline_at: float = 0.0,
    dates: dict[str, str] | None = None,
    resolved: dict[str, str] | None = None,
    settle_ms: int = 5000,
) -> str:
    """打开 Google News 跳转页，等它落到发布方，再抽取正文。

    成功时把真实 URL 写进 resolved[url]，供上层把 raw_docs.url 换成真地址，
    这样报告页的「原文链接」也不再是 Google 中转页。

    跳转是 JS 触发的，实测有的链接 1.5 秒内就跳、有的要 2.5 秒——
    原来固定等 2.5 秒正好卡在临界点上，差几十毫秒就判失败（C-16）。
    现在改成小步轮询，一跳到就走，最多等到 settle_ms。
    """
    if deadline_at and time.time() > deadline_at:
        return ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        final = ""
        stop_at = time.time() + max(1.0, settle_ms / 1000.0)
        while True:
            page.wait_for_timeout(300)
            final = page.url
            host = (urlparse(final).hostname or "").lower() if final else ""
            if final and GOOGLE_NEWS_HOST not in host:
                break
            if time.time() >= stop_at:
                log.debug("Google News 跳转未还原：%s", url[:60])
                return ""
        html = page.content()
        if dates is not None:
            published = extract_date_published(html)
            if published:
                dates[url] = published
        text = extract_text(html)
        # Cloudflare 的「Just a moment」通常几秒后自己放行，等一轮再抽一次（C-16）
        if looks_blocked(text):
            page.wait_for_timeout(6000)
            html = page.content()
            if dates is not None and url not in dates:
                published = extract_date_published(html)
                if published:
                    dates[url] = published
            text = extract_text(html)
        if not text or not looks_like_prose(text):
            # 落地页本身就短（如 BoLS 被 Cloudflare 挡）时，至少把真实 URL 记下来，
            # 下一轮就能直接按发布方地址 HTTP 抓，不用再走一遍跳转
            if resolved is not None:
                resolved[url] = final
            return ""
        if resolved is not None:
            resolved[url] = final
        log.debug("Google News 还原 -> %s（%d 字）", final[:70], len(text))
        return text
    except Exception as exc:  # noqa: BLE001
        log.debug("Google News 还原失败 %s: %s", url[:60], str(exc)[:60])
        return ""


def backfill_short_docs(cfg: Any, db: Any, limit: int = 20) -> dict[str, Any]:
    """给已分析但正文过短的文档补抓一次。

    只处理「够得着的」：deep_fetch 源，或开启 Google News 还原时的 googlenews 源。
    补到正文明显变长后，把 analyzed_at 置空，让下一轮重新分析——否则正文升级了、
    摘要还是旧的一句话，用户看不到任何变化。
    """
    stats = {"candidates": 0, "fetched": 0, "requeued": 0}
    if not cfg.get("content.backfill_enabled", True):
        return stats

    min_body = int(cfg.get("content.backfill_min_body", 400) or 0)
    days = int(cfg.get("content.backfill_days", 7) or 7)
    deep_sources = {s["id"] for s in cfg.sources if s.get("deep_fetch")}
    if cfg.get("content.resolve_google_news", True):
        deep_sources |= {s["id"] for s in cfg.sources if s.get("type") == "googlenews"}
    if not deep_sources:
        return stats

    from datetime import datetime as _dt, timedelta as _td

    from .db import CST as _CST

    since = (_dt.now(_CST) - _td(days=days)).replace(microsecond=0).isoformat()
    marks = ",".join("?" * len(deep_sources))
    rows = db.conn.execute(
        f"""
        SELECT r.*, COALESCE(r.derived_tier, s.tier) AS tier,
               s.name AS source_name, s.category, s.weight AS source_weight,
               COALESCE(MAX(st.importance_total), 0) AS story_score
        FROM raw_docs r
        JOIN sources s ON s.id = r.source_id
        LEFT JOIN story_docs sd ON sd.raw_doc_id = r.id
        LEFT JOIN stories st ON st.id = sd.story_id
        WHERE r.analyzed_at IS NOT NULL
          AND length(coalesce(r.body,'')) < ?
          AND COALESCE(r.discovered_at, r.published_at, r.fetched_at) >= ?
          AND r.source_id IN ({marks})
        GROUP BY r.id
        -- 先救「已经入选候补却只有标题」的高分事件，再按层级、时间兜底（C-20）。
        -- 原来的排序只看层级+时间，55 分但没正文的条目可能一直排在队尾。
        ORDER BY story_score DESC,
                 CASE COALESCE(r.derived_tier, s.tier)
                   WHEN 'L0' THEN 0 WHEN 'L1' THEN 1 WHEN 'L2' THEN 2 ELSE 3 END,
                 COALESCE(r.discovered_at, r.published_at, r.fetched_at) DESC
        LIMIT ?
        """,
        (min_body, since, *sorted(deep_sources), max(1, limit)),
    ).fetchall()
    stats["candidates"] = len(rows)
    if not rows:
        return stats

    before = {
        r["id"]: len((r["body"] or "").strip()) for r in rows
    }
    log.info("=== 补抓：已分析但正文过短的文档 %d 条 ===", len(rows))
    enrich_docs(cfg, db, [dict(r) for r in rows])

    if cfg.get("content.backfill_reanalyze", True):
        with db.tx() as conn:
            for rid, old_len in before.items():
                now_len = conn.execute(
                    "SELECT length(coalesce(body,'')) n FROM raw_docs WHERE id=?", (rid,)
                ).fetchone()
                new_len = (now_len["n"] if now_len else 0) or 0
                # 正文至少翻倍且超过阈值，才值得再花一次 LLM
                if new_len >= max(min_body, old_len * 2):
                    conn.execute("UPDATE raw_docs SET analyzed_at=NULL WHERE id=?", (rid,))
                    stats["requeued"] += 1
    stats["fetched"] = stats["requeued"]
    if stats["requeued"]:
        log.info("补抓后重新排队分析 %d 条（下一轮生效）", stats["requeued"])
    return stats
