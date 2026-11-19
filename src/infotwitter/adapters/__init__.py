"""采集适配器。"""
from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

import feedparser

from ..db import CST, now_iso, parse_dt
from ..httpclient import Fetcher, url_host
from .bilibili import BilibiliAdapter
from .nga import NgaAdapter
from .reddit import RedditAdapter
from .tieba import TiebaAdapter

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# 已知媒体域名 → 可信层级（用于 Google News 聚合条目的动态定级）
DOMAIN_TIER: dict[str, str] = {
    "warhammer-community.com": "L0",
    "warhammer.com": "L0",
    "games-workshop.com": "L0",
    "goonhammer.com": "L1",
    "wargamer.com": "L1",
    "belloflostsouls.net": "L1",
    "spikeybits.com": "L1",
    "polygon.com": "L1",
    "pcgamer.com": "L1",
    "eurogamer.net": "L1",
    "ign.com": "L1",
    "rockpapershotgun.com": "L1",
    "gcores.com": "L2",
    "yystv.cn": "L2",
    "3dmgame.com": "L2",
    "gamersky.com": "L2",
    "reddit.com": "L2",
    "4chan.org": "L3",
}


class Adapter(Protocol):
    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]]: ...


def clean_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    # 列表页锚文本常带 "1. " 之类的序号，去掉以免污染标题
    text = re.sub(r"^\d{1,2}[.)、]\s*", "", text)
    return text.strip()


def content_hash(title: str, url: str) -> str:
    key = f"{WS_RE.sub(' ', (title or '').strip().lower())}|{url.strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def is_valid_feed(parsed: Any) -> bool:
    """判断 feedparser 的结果是真 feed 还是被反爬页/错误页顶替。

    实测：Cloudflare 挑战页以 HTTP 200 返回时，feedparser 给出
    bozo=False / entries=0 / version=''，与「合法 feed 但窗口内 0 条」无法区分。
    不校验的话整条 RSS 通道会在「全源正常」的假象下失效（核查报告 BUG-09）。
    """
    return bool(parsed.get("version")) or bool(parsed.entries)


def to_iso(entry: Any, fallback: datetime | None = None) -> str | None:
    """取 feed 里的发布时间；**取不到就返回 None，绝不拿"现在"顶替**。

    以前这里 fallback 到 datetime.now()，而 RSS/GoogleNews 适配器又不写
    time_source，DB 默认填 'feed'；于是「feed 没给日期」会被包装成
    「这是刚发布的、且来源是 feed」——直接绕过 pipeline 的旧文守卫
    （守卫只拦 time_source=='fetch' 且 published 为空）。
    实测合成 feed：21 天前的条目入库后 published_at=now，进了当日池。
    拿不到时间就是拿不到，交给上层按 time_source='fetch' 降级。
    """
    for attr in ("published_parsed", "updated_parsed"):
        value = entry.get(attr)
        if value:
            try:
                dt = datetime(*value[:6], tzinfo=timezone.utc).astimezone(CST)
                return dt.replace(microsecond=0).isoformat()
            except (TypeError, ValueError):
                continue
    if fallback is not None:
        return fallback.replace(microsecond=0).isoformat()
    return None


def _published_or_fetch(raw: Any) -> tuple[str | None, str]:
    """把列表页里的 datetime 属性/文本转成 ISO；拿不到就返回 (None, 'fetch')。

    返回 (published_at, time_source)，time_source=article|fetch。
    **拿不到时间时不能拿抓取时间顶替**：那会把三周前的旧文标成"刚刚发布"，
    直接骗过 24h 推送窗口与时效计分（核查报告 C-01）。
    """
    text = str(raw or "").strip()
    if text:
        dt = _parse_published_time(text)
        if dt is not None:
            return dt.isoformat(), "article"
    return None, "fetch"


# 列表页常见的日期写法。宁可少认，也不要认错。
_DATE_FORMATS = (
    "%a %d %b %y",     # Mon 16 Nov 26
    "%a %d %b %Y",     # Mon 16 Nov 2026
    "%d %b %y",        # 16 Nov 26
    "%d %b %Y",        # 16 Nov 2026
    "%d %B %Y",        # 16 November 2026
    "%b %d %Y",        # Nov 16 2026（逗号已在上一步去掉）
    "%B %d %Y",        # November 16 2026
    "%d/%m/%Y",        # 16/11/2026
    "%Y-%m-%d",        # 2026-11-16
    "%Y/%m/%d",        # 2026/11/16
)


def _parse_published_time(raw: str) -> datetime | None:
    """解析站点给出的日期属性或文本；解析不了返回 None（调用方必须当"未知"处理）。"""
    text = (raw or "").strip()
    if not text:
        return None
    # 1) 机器可读格式（ISO / RFC）
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).replace(microsecond=0)
    except ValueError:
        pass
    # 2) RFC 1123（部分站点用）——用 email.utils 解析，避免再引依赖
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CST).replace(microsecond=0)
    except (TypeError, ValueError):
        pass
    # 3) 常见人读格式
    cleaned = re.sub(r"\s+", " ", text).replace(",", "").replace("，", "").strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        # 两位年份：站点都是 20xx
        if dt.year < 100:
            dt = dt.replace(year=2000 + dt.year)
        return dt.replace(tzinfo=CST)
    return None


def _keyword_hit(source: dict[str, Any], *texts: str) -> bool:
    keywords = [k.lower() for k in (source.get("keywords") or [])]
    if not keywords:
        return True
    blob = " ".join(t for t in texts if t).lower()
    return any(k in blob for k in keywords)


# 社区讨论帖的典型形态：提问、求建议、晒图，几乎不含新闻价值
QUESTION_RE = re.compile(
    r"^\s*(what|how|why|which|should|can|is|does|do|any|anyone|when|where|who|"
    r"help|newbie|noob|beginner)\b",
    re.I,
)


def passes_filters(source: dict[str, Any], title: str) -> bool:
    """按源配置过滤掉低信息量的社区帖。"""
    cfg = source.get("filter") or {}
    if not cfg:
        return True

    min_len = int(cfg.get("min_title_len", 0))
    if min_len and len(title) < min_len:
        return False

    for suffix in cfg.get("reject_suffix") or []:
        if title.rstrip().endswith(suffix):
            return False

    if cfg.get("reject_questions") and QUESTION_RE.match(title):
        return False

    for pattern in cfg.get("reject_regex") or []:
        if re.search(pattern, title, re.I):
            return False
    return True


class RssAdapter:
    """直接订阅 RSS/Atom。"""

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
        resp = self.fetcher.get(
            source["url"],
            headers=source.get("headers"),
            min_gap=float(source.get("min_gap", 1.5)),
            # RSS 是小响应，不需要 30s 的读超时；压到 15s 避免
            # "30s×4 次 = 单 URL 两分钟"（C-10）
            read_timeout=15.0,
        )
        if resp is None or resp.status_code >= 400:
            return None
        parsed = feedparser.parse(resp.content)
        if not is_valid_feed(parsed):
            log.warning("源 %s 返回的不是有效 feed（多半被反爬页顶替）", source["id"])
            return None
        out: list[dict[str, Any]] = []
        for entry in parsed.entries:
            published = to_iso(entry)
            # 没有日期就如实标注：time_source='fetch' 才能让上游的旧文守卫生效
            time_source = "feed" if published else "fetch"
            if published:
                pub_dt = datetime.fromisoformat(published)
                if pub_dt < since:
                    continue
            title = clean_text(entry.get("title"))
            summary = clean_text(entry.get("summary") or entry.get("description"))
            link = entry.get("link") or source["url"]
            if not title:
                continue
            if not _keyword_hit(source, title, summary):
                continue
            if not passes_filters(source, title):
                continue
            out.append(
                {
                    "source_id": source["id"],
                    "url": link,
                    "title": title,
                    "body": summary,
                    "lang": source.get("lang", "en"),
                    "published_at": published,
                    "fetched_at": now_iso(),
                    "content_hash": content_hash(title, link),
                    "time_source": time_source,
                }
            )
        return out


class GoogleNewsAdapter:
    """Google News RSS 定向查询。

    实测：官网/商店被 Cloudflare 拦截时，用 site: 查询仍能拿到其内容；
    但条目 link 是 Google 跳转地址，无法在不解析 batchexecute 的情况下还原原文 URL，
    因此同时记录来源域名，供展示与定级使用。
    """

    BASE = "https://news.google.com/rss/search"

    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
        import urllib.parse

        query = source.get("query") or source["name"]
        url = (
            f"{self.BASE}?q={urllib.parse.quote(query)}"
            f"&hl={source.get('hl', 'en-US')}&gl={source.get('gl', 'US')}"
            f"&ceid={urllib.parse.quote(source.get('ceid', 'US:en'))}"
        )
        resp = self.fetcher.get(
            url,
            headers=source.get("headers"),
            min_gap=float(source.get("min_gap", 1.5)),
            read_timeout=15.0,
        )
        if resp is None or resp.status_code >= 400:
            return None
        parsed = feedparser.parse(resp.content)
        if not is_valid_feed(parsed):
            log.warning("源 %s 返回的不是有效 feed（多半被反爬页顶替）", source["id"])
            return None
        out: list[dict[str, Any]] = []
        for entry in parsed.entries:
            published = to_iso(entry)
            time_source = "feed" if published else "fetch"
            if published:
                pub_dt = datetime.fromisoformat(published)
                if pub_dt < since:
                    continue
            raw_title = clean_text(entry.get("title"))
            summary = clean_text(entry.get("summary"))
            link = entry.get("link") or ""
            if not raw_title or not link:
                continue

            src_name, src_domain = "", ""
            src = entry.get("source")
            if isinstance(src, dict):
                src_name = src.get("title") or ""
                src_domain = src.get("href") or ""
            src_domain = re.sub(r"^https?://(www\.)?", "", src_domain).strip("/").lower()

            # Google News 标题形如 "标题 - 来源名"，剥掉尾巴
            title = raw_title
            if src_name and title.endswith(f" - {src_name}"):
                title = title[: -(len(src_name) + 3)].strip()

            # 依据真实来源域名做二次定级。
            # 关键：拿不到发布域名时**不能**继承查询源的 tier——`gnews_gw_results`(L1)
            # 会把 uk.investing.com / thisismoney.co.uk 这类站点一律按 L1 计权威。
            # 只有 site: 限定的查询才天然知道发布方是谁，可以沿用配置里的层级。
            query = str(source.get("query") or "").lower()
            site_restricted = "site:" in query
            tier = DOMAIN_TIER.get(src_domain)
            if tier is None:
                tier = source.get("tier", "L2") if site_restricted else "L2"

            if not _keyword_hit(source, title, summary):
                continue

            out.append(
                {
                    "source_id": source["id"],
                    "url": link,
                    "title": title,
                    "body": summary,
                    "lang": source.get("lang", "en"),
                    "published_at": published,
                    "fetched_at": now_iso(),
                    "content_hash": content_hash(title, src_domain or link),
                    "publisher": src_name,
                    "publisher_domain": src_domain,
                    "derived_tier": tier,
                    "time_source": time_source,
                }
            )
        return out


class BrowserAdapter:
    """Playwright 渲染后抽取链接（用于 Cloudflare 保护的官网）。"""

    def __init__(self, proxy: str | None, user_agent: str, fetcher: Fetcher | None = None):
        self.proxy = proxy
        self.user_agent = user_agent
        # 复用 Fetcher 的域名熔断，避免代理整体故障时每个源各耗满 35s（BUG-24）
        self.breaker = fetcher

    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
        from playwright.sync_api import sync_playwright

        pattern = source.get("link_pattern") or "/articles/"
        urls = [source["url"], *(source.get("fallback_urls") or [])]
        host = url_host(urls[0])
        if self.breaker and self.breaker.should_skip_host(host):
            log.warning("浏览器源 %s 跳过（域名 %s 熔断中）", source["id"], host)
            return None
        out: list[dict[str, Any]] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                proxy={"server": self.proxy} if self.proxy else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                ctx = browser.new_context(
                    user_agent=self.user_agent,
                    locale="en-GB",
                    viewport={"width": 1440, "height": 1000},
                )
                page = ctx.new_page()
                loaded = False
                for url in urls:
                    # 代理偶发抖动，同一 URL 最多试 2 次（指数退避）
                    url_ok = False
                    for attempt in range(2):
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=35000)
                            page.wait_for_timeout(2000)
                            if "just a moment" in (page.title() or "").lower():
                                # Cloudflare 挑战不是"打不开"，多等一会儿通常能过；
                                # 以前这里直接 break，等于对该站点零重试（C-04）
                                log.warning(
                                    "浏览器源 %s 遇到 Cloudflare 挑战（第 %d 次）%s",
                                    source["id"], attempt + 1, url,
                                )
                                if attempt == 0:
                                    page.wait_for_timeout(3000)
                                    continue
                                break
                            url_ok = True
                            break
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "浏览器源 %s 打开失败（第 %d 次）%s: %s",
                                source["id"], attempt + 1, url, str(exc)[:80],
                            )
                            if attempt == 0:
                                import time as _t

                                _t.sleep(1.5)
                    if url_ok:
                        if self.breaker:
                            self.breaker.mark_host_ok(host)
                        log.info("浏览器源 %s 命中 %s", source["id"], url)
                        loaded = True
                        break
                if not loaded:
                    # 整个源只计一次失败：以前每次重试都 mark，一个源就能把共享域名熔断（C-05）
                    if self.breaker:
                        self.breaker.mark_host_fail(host)
                    return None

                items = page.eval_on_selector_all(
                    "a[href]",
                    """els => els.map(e => {
                        // 日期可能写在 <time datetime>，也可能只写在 <time> 的文本里
                        // （Warhammer Community 就是后者）。先取属性，再退到文本。
                        //
                        // 关键：不能对祖先整棵子树 querySelector——侧栏「最多阅读」里的
                        // 卡片也在这个祖先里，会抓到别的文章的日期（实测 6 条错 4 条）。
                        // 只接受「属于本卡片」的 <time>：要么它最近的 <a> 就是当前锚点，
                        // 要么这一层容器里只有当前这一个文章链接。
                        const ARTICLE = 'a[href*="/articles/"]';
                        let node = e, dt = '';
                        for (let d = 0; node && d < 6; d++, node = node.parentElement) {
                            const t = node.querySelector('time, [datetime], [data-date]');
                            if (t) {
                                const owner = t.closest(ARTICLE);
                                const links = node.querySelectorAll(ARTICLE).length;
                                if (owner === e || links === 1) {
                                    dt = t.getAttribute('datetime')
                                      || t.getAttribute('data-date')
                                      || (t.textContent || '').trim();
                                    if (dt) break;
                                }
                            }
                            // 一旦这一层已经包含多个文章链接，往上只会更容易串卡片
                            if (node.querySelectorAll(ARTICLE).length > 1 && d > 0) break;
                        }
                        return {href: e.href, text: (e.textContent||'').trim(), datetime: dt};
                    })""",
                )
                excludes = source.get("exclude_slugs") or []
                seen: set[str] = set()
                skipped_old = 0
                undated = 0
                for item in items:
                    href = item.get("href") or ""
                    text = clean_text(item.get("text"))
                    if pattern not in href or href in seen:
                        continue
                    if any(bad in href for bad in excludes):
                        continue
                    if len(text) < 8:
                        continue
                    seen.add(href)
                    published, time_source = _published_or_fetch(item.get("datetime"))
                    if published is None:
                        # 日期解析不出来 → 保留（可能是有效内容），但标记为「时间未知」，
                        # 由选槽层排除出「今日新闻」范畴（见 pipeline.select_slots）
                        undated += 1
                    else:
                        # 必须真正用 since 过滤：之前这个参数收进来却从未使用
                        pub_dt = parse_dt(published)
                        if pub_dt is not None and pub_dt < since:
                            skipped_old += 1
                            continue
                    out.append(
                        {
                            "source_id": source["id"],
                            "url": href,
                            "title": text[:200],
                            "body": "",
                            "lang": source.get("lang", "en"),
                            "published_at": published,
                            "fetched_at": now_iso(),
                            "content_hash": content_hash(text, href),
                            "publisher_domain": urlparse(href).netloc.lower(),
                            "time_source": time_source,
                        }
                    )
                if skipped_old or undated:
                    log.info(
                        "浏览器源 %s：窗口外跳过 %d 条，日期解析失败 %d 条",
                        source["id"], skipped_old, undated,
                    )
            finally:
                browser.close()
        return out


def build_adapters(fetcher: Fetcher, cfg: Any) -> dict[str, Any]:
    return {
        "rss": RssAdapter(fetcher),
        "googlenews": GoogleNewsAdapter(fetcher),
        "browser": BrowserAdapter(cfg.proxy, cfg.user_agent, fetcher),
        "bilibili": BilibiliAdapter(cfg.proxy, cfg.user_agent, fetcher),
        "tieba": TiebaAdapter(cfg.proxy, cfg.user_agent),
        "nga": NgaAdapter(cfg.proxy, cfg.user_agent),
        "reddit": RedditAdapter(cfg.proxy, cfg.user_agent),
    }


def collect(
    cfg: Any, db: Any, extra_fetcher: Fetcher | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """遍历所有启用源并采集，返回 (新增 raw_docs, 失败源列表)。"""
    from ..httpclient import make_fetcher

    fetcher = extra_fetcher or make_fetcher(cfg)
    adapters = build_adapters(fetcher, cfg)

    all_docs: list[dict[str, Any]] = []
    failed: list[str] = []
    for source in cfg.active_sources():
        adapter = adapters.get(source["type"])
        if adapter is None:
            log.warning("未知适配器类型 %s (%s)", source["type"], source["id"])
            continue
        # 中文社区消息稀少，允许单源放宽采集窗口（推送仍按 published_at 判新鲜度）
        hours = int(source.get("collect_hours", cfg.collect_hours))
        since = (datetime.now(CST) - timedelta(hours=hours)).replace(microsecond=0)
        try:
            docs = adapter.fetch(source, since)
        except Exception as exc:  # noqa: BLE001
            log.error("源 %s 采集异常: %s", source["id"], exc)
            docs = None
        if docs is None:
            # 真正的失败：网络错误、被拦截、浏览器没加载出来
            failed.append(source["id"])
            db.mark_source_result(source["id"], False)
            log.warning("源 %-18s 采集失败", source["id"])
        elif not docs:
            # 空结果是正常现象（窗口内确实没有新内容），不算失败
            db.mark_source_result(source["id"], True)
            log.info("源 %-18s 窗口内无新内容", source["id"])
        else:
            all_docs.extend(docs)
            db.mark_source_result(source["id"], True)
            log.info("源 %-18s 采集 %d 条", source["id"], len(docs))

    if not extra_fetcher:
        fetcher.close()
    return all_docs, failed
