"""B 站适配器：搜索 API 需要 cookie 指纹（否则 412），用真实浏览器取一次 cookie 再调接口。

实测：直接请求返回 412；Playwright 访问一次 bilibili.com 拿到 buvid3 等 cookie 后，
`/x/web-interface/search/type` 正常返回 code=0，含发布时间与播放量。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..db import CST, now_iso

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]+>")
CACHE_PATH = Path(__file__).resolve().parents[2].parent / "data" / "cookies_bili.json"
COOKIE_TTL = 6 * 3600  # cookie 复用 6 小时


def _strip_tags(text: str) -> str:
    return TAG_RE.sub("", text or "").strip()


class BilibiliAdapter:
    def __init__(self, proxy: str | None, user_agent: str, fetcher: Any | None = None):
        self.proxy = proxy
        self.user_agent = user_agent
        # 复用共享的域名熔断（BUG-24）；没有传入时退化为无熔断
        self.breaker = fetcher

    # ---------- cookie ----------
    def _harvest_cookies(self) -> dict[str, str]:
        from playwright.sync_api import sync_playwright

        log.info("B站：用浏览器获取 cookie")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                proxy={"server": self.proxy} if self.proxy else None,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                ctx = browser.new_context(user_agent=self.user_agent, locale="zh-CN")
                page = ctx.new_page()
                page.goto("https://www.bilibili.com", wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(2500)
                cookies = ctx.cookies()
            finally:
                browser.close()

        jar = {c["name"]: c["value"] for c in cookies}
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"ts": int(time.time()), "cookies": jar}, ensure_ascii=False),
            encoding="utf-8",
        )
        return jar

    def _cookies(self) -> dict[str, str]:
        if CACHE_PATH.exists():
            try:
                data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    jar, ts = data.get("cookies") or {}, float(data.get("ts") or 0)
                elif isinstance(data, list):  # 兼容早期版本存的原始 cookie 列表
                    jar = {c["name"]: c["value"] for c in data if isinstance(c, dict) and c.get("name")}
                    ts = CACHE_PATH.stat().st_mtime
                else:
                    jar, ts = {}, 0.0
                if jar and time.time() - ts < COOKIE_TTL:
                    return jar
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        return self._harvest_cookies()

    # ---------- fetch ----------
    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]] | None:
        if since.tzinfo is None:  # 防御：调用方传了裸 datetime
            since = since.replace(tzinfo=CST)
        keyword = source.get("keyword") or "战锤"
        limit = int(source.get("max_items", 20))
        headers = {
            "User-Agent": self.user_agent,
            "Referer": "https://search.bilibili.com/",
            "Accept": "application/json",
        }
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": 1,
            "order": "pubdate",
        }

        for attempt in range(2):
            if self.breaker and self.breaker.should_skip_host("api.bilibili.com"):
                log.warning("B 站接口域名熔断中，跳过 %s", source.get("id"))
                return None
            jar = self._cookies()
            network_error = False
            try:
                with httpx.Client(
                    proxy=self.proxy, timeout=25.0, follow_redirects=True,
                    cookies=jar, headers=headers,
                ) as client:
                    resp = client.get(
                        "https://api.bilibili.com/x/web-interface/search/type", params=params
                    )
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                log.warning("B站接口异常（第 %d 次）：%s", attempt + 1, str(exc)[:80])
                payload = {}
                network_error = True

            if payload.get("code") == 0:
                if self.breaker:
                    self.breaker.mark_host_ok("api.bilibili.com")
                break

            code = payload.get("code")
            if network_error:
                # 网络异常不是"cookie 失效"，删缓存只会让下次白白再开一次浏览器（C-06）
                log.warning("B站网络异常，保留 cookie（第 %d 次）", attempt + 1)
                if self.breaker:
                    self.breaker.mark_host_fail("api.bilibili.com")
                continue

            log.warning("B站返回 code=%s，刷新 cookie 重试", code)
            if code in (-412, -401, 412):
                # 只有鉴权类错误才值得重新取 cookie
                if self.breaker:
                    self.breaker.mark_host_fail("api.bilibili.com")
                if CACHE_PATH.exists():
                    CACHE_PATH.unlink(missing_ok=True)
            else:
                # -500 之类的服务端错误：重试一次，但别把可用 cookie 一起扔了
                log.warning("B站服务端错误 code=%s，保留 cookie", code)
        else:
            return None

        items = (payload.get("data") or {}).get("result") or []
        out: list[dict[str, Any]] = []
        for v in items[:limit]:
            if not isinstance(v, dict):
                continue
            title = _strip_tags(v.get("title") or "")
            bvid = v.get("bvid")
            if not title or not bvid:
                continue
            pubdate = v.get("pubdate")
            published: datetime | None
            try:
                published = datetime.fromtimestamp(int(pubdate), tz=CST)
            except (TypeError, ValueError, OSError):
                # 拿不到发布时间就标记为未知，不要拿 now() 顶替（C-01 同类问题）
                published = None
            if published is not None and published < since:
                continue
            url = f"https://www.bilibili.com/video/{bvid}"
            out.append(
                {
                    "source_id": source["id"],
                    "url": url,
                    "title": title,
                    "body": _strip_tags(v.get("description") or ""),
                    "lang": "zh",
                    "published_at": (
                        published.replace(microsecond=0).isoformat() if published else None
                    ),
                    "fetched_at": now_iso(),
                    "content_hash": _hash(title, url),
                    "publisher_domain": "bilibili.com",
                    "time_source": "article" if published else "fetch",
                    "engagement": _safe_int(v.get("play")),
                }
            )
        # 降噪：B 站视频的简介本身就是一句话（实测 1–279 字），没法靠"加深"补救；
        # 而 20 条里绝大多数播放量只有几十。按播放量取每源前 N 条（C-21）。
        out = _top_by_play(out, int(source.get("top_n", 0) or 0), source.get("id"))
        # 搜索接口的 description 只有一句话（实测上限 264 字），
        # view 接口能拿到完整简介（实测 174~455 字），对摘要质量影响明显
        _fill_descriptions(
            out,
            proxy=self.proxy,
            headers=headers,
            jar=self._cookies(),
            breaker=self.breaker,
            limit=int(source.get("desc_lookup", 10) or 0),
        )
        return out


def _top_by_play(
    docs: list[dict[str, Any]], top_n: int, source_id: str | None = None
) -> list[dict[str, Any]]:
    """按播放量保留前 top_n 条，再按发布时间倒序返回（C-21）。

    绝对阈值不适用：实测战锤关键词下播放量中位数只有几十，最高不到 4 千，
    卡阈值会把内容全杀掉；按源取前 N 才是稳定的降噪方式。
    """
    if top_n <= 0 or len(docs) <= top_n:
        return docs
    before = len(docs)
    ranked = sorted(
        docs, key=lambda d: (-int(d.get("engagement") or 0), d.get("published_at") or "")
    )[:top_n]
    ranked.sort(key=lambda d: d.get("published_at") or "", reverse=True)
    log.info("B站 %s：按播放量取前 %d 条（原 %d 条）", source_id or "?", top_n, before)
    return ranked


def _safe_int(value: Any) -> int:
    """B 站偶尔返回 '1.2万' / '--' 这类非数字，单条脏数据不应拖垮整批（BUG-43）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10_000)
        if text.endswith("亿"):
            return int(float(text[:-1]) * 100_000_000)
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _hash(title: str, url: str) -> str:
    import hashlib

    # 注意：Python ≤3.11 的 f-string 表达式里不允许出现反斜杠（PEP 701 之前），
    # 所以正则必须先在 f-string 外面算好（核查报告 C-02）
    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    key = f"{norm}|{url.strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


VIEW_API = "https://api.bilibili.com/x/web-interface/view"


def _fill_descriptions(
    items: list[dict[str, Any]],
    *,
    proxy: str | None,
    headers: dict[str, str],
    jar: Any,
    breaker: Any = None,
    limit: int = 10,
) -> int:
    """按 bvid 调 view 接口拿完整简介，写回 body。

    只补前 limit 条：每条一次请求，不宜无限放大。
    任何一条失败就停止，避免在风控状态下硬刷。
    """
    if limit <= 0 or not items:
        return 0
    if breaker and breaker.should_skip_host("api.bilibili.com"):
        log.debug("B 站接口熔断中，跳过简介补齐")
        return 0

    filled = 0
    try:
        with httpx.Client(
            proxy=proxy, timeout=15.0, follow_redirects=True,
            cookies=jar, headers=headers,
        ) as client:
            for item in items[:limit]:
                bvid = str(item.get("url") or "").rsplit("/", 1)[-1]
                if not bvid.startswith("BV"):
                    continue
                try:
                    payload = client.get(VIEW_API, params={"bvid": bvid}).json()
                except Exception as exc:  # noqa: BLE001
                    log.debug("B 站简介接口失败 %s: %s", bvid, str(exc)[:50])
                    break
                if payload.get("code") != 0:
                    log.debug("B 站简介接口 code=%s，停止补齐", payload.get("code"))
                    break
                data = payload.get("data") or {}
                desc = _strip_tags(data.get("desc") or "")
                if desc and len(desc) > len(item.get("body") or ""):
                    item["body"] = desc
                    filled += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("B 站简介补齐整体失败：%s", str(exc)[:80])
        return filled
    if filled:
        log.debug("B 站补齐简介 %d 条", filled)
    return filled
