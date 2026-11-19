"""Reddit 适配器：浏览器渲染（RSS/JSON 端点已被封）。

为什么不用 `.rss`：实测 `www.reddit.com/r/x/.rss` 返回 429/404，
`old.reddit.com` 返回的是 HTML 而不是 XML，`.json` 403。
但用 Playwright 渲染 `r/<sub>/new/` 完全正常（约 4 秒）。

时间戳：Reddit 的帖子卡片里有 `<time datetime>` / `<faceplate-timeago ts>`，
向上找 8 层祖先即可命中（实测 34/34 全部拿到）。没有时间的话，
非 L0 内容会被「无发布时间守卫」挡在通选槽之外，等于白抓。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..db import CST, now_iso

log = logging.getLogger(__name__)

_EXTRACT_JS = """() => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('a[href*="/comments/"]').forEach(a => {
        const href = a.href.split('?')[0];
        if (!href || seen.has(href)) return;
        seen.add(href);
        let node = a, depth = 0, ts = null;
        while (node && depth < 8) {
            const t = node.querySelector('time[datetime], faceplate-timeago[ts]');
            if (t) { ts = t.getAttribute('datetime') || t.getAttribute('ts'); break; }
            node = node.parentElement; depth++;
        }
        const post = a.closest('shreddit-post');
        out.push({
            href: href,
            title: (a.innerText || '').trim(),
            ts: ts,
            // 只有 text 类型才有自述正文，图片/图集/链接帖没有（C-17）
            kind: post ? (post.getAttribute('post-type') || '') : '',
        });
    });
    return out;
}"""

# 帖子页的自述正文容器
_BODY_JS = """() => {
    const p = document.querySelector('shreddit-post');
    if (!p) return '';
    const body = p.querySelector('[slot="text-body"], div[data-post-click-location="text-body"]');
    return body ? (body.innerText || '').trim() : '';
}"""

_TID_RE = re.compile(r"/comments/([a-z0-9]+)/")


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    # faceplate-timeago 用 +0000 而不是 +00:00
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CST)


class RedditAdapter:
    def __init__(self, proxy: str | None = None, user_agent: str = "") -> None:
        self.proxy = proxy
        self.user_agent = user_agent

    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]] | None:
        sub = str(source.get("subreddit") or "").strip()
        if not sub:
            log.warning("源 %s 缺少 subreddit", source.get("id"))
            return None

        from playwright.sync_api import sync_playwright

        url = f"https://www.reddit.com/r/{sub}/new/"
        limit = int(source.get("max_items", 40) or 40)
        body_lookup = int(source.get("body_lookup", 0) or 0)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    proxy={"server": self.proxy} if self.proxy else None,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    ctx = browser.new_context(
                        user_agent=self.user_agent or None,
                        locale="en-US",
                        viewport={"width": 1440, "height": 1200},
                    )
                    page = ctx.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(3500)
                    # 滚一屏触发懒加载，否则只有前几条有时间戳
                    page.mouse.wheel(0, 2200)
                    page.wait_for_timeout(2500)
                    cards = page.evaluate(_EXTRACT_JS) or []
                    out = self._build_docs(source, cards, sub, since, limit)
                    text_posts = sum(1 for d in out if d.get("_has_body"))
                    if body_lookup:
                        self._fill_bodies(page, out, body_lookup)
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Reddit r/%s 抓取失败：%s", sub, str(exc)[:80])
            return None

        filled = sum(1 for d in out if d.get("_body_filled"))
        for doc in out:
            doc.pop("_has_body", None)
            doc.pop("_body_filled", None)
        log.info(
            "Reddit r/%s：%d 帖，窗口内 %d 条（文本帖 %d，补正文 %d）",
            sub, len(cards), len(out), text_posts, filled,
        )
        return out

    def _build_docs(
        self, source: dict[str, Any], cards: list[dict[str, Any]],
        sub: str, since: datetime, limit: int,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for card in cards:
            href = str(card.get("href") or "")
            # 链接文本里常混入 "2.2K vote" 之类的行，只取第一行
            title = str(card.get("title") or "").splitlines()[0].strip() if card.get("title") else ""
            m = _TID_RE.search(href)
            if not title or not m or len(title) < 5:
                continue
            published = _parse_ts(card.get("ts"))
            if published is not None and published < since:
                continue
            out.append({
                "source_id": source["id"],
                "url": href,
                "title": title,
                "body": f"{title}（Reddit r/{sub}）",
                "lang": "en",
                "published_at": published.replace(microsecond=0).isoformat() if published else None,
                "fetched_at": now_iso(),
                "content_hash": _hash(title, m.group(1)),
                "publisher_domain": "reddit.com",
                "time_source": "article" if published else "fetch",
                "engagement": 0,
                "_has_body": str(card.get("kind") or "") == "text",
            })
            if len(out) >= limit:
                break
        return out

    def _fill_bodies(self, page: Any, docs: list[dict[str, Any]], limit: int) -> None:
        """给文本帖补自述正文（图片/图集帖没有正文，不去白跑）。"""
        filled = 0
        for doc in docs:
            if filled >= limit:
                break
            if not doc.get("_has_body"):
                continue
            try:
                page.goto(doc["url"], wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1200)
                body = page.evaluate(_BODY_JS) or ""
            except Exception as exc:  # noqa: BLE001
                log.debug("Reddit 正文失败 %s: %s", doc["url"][:60], str(exc)[:50])
                continue
            body = str(body).strip()
            if len(body) > 40:
                doc["body"] = body[:4000]
                doc["_body_filled"] = True
                filled += 1


def _hash(title: str, tid: str) -> str:
    import hashlib

    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha1(f"{norm}|{tid}".encode("utf-8")).hexdigest()
