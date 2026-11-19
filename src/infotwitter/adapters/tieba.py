"""百度贴吧适配器（基于 aiotieba，匿名可读，不需要账号）。

为什么不用 TiebaScraper：那套工具要求 PostgreSQL + Redis + uvloop，
对一个 SQLite 单机项目太重。它底层的 aiotieba 是一个纯 Python 库，
直接读公开版块就能满足需求（实测战锤吧/战棋吧/模型吧均可用，无需登录）。

信息价值：贴吧帖子以问答、灌水为主，只有少数「新闻/发售/规则」类帖有情报价值，
因此用 keywords 做正向过滤——命中任一关键词才收录，避免把水帖灌进流水线。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from ..db import CST, now_iso

log = logging.getLogger(__name__)


class TiebaAdapter:
    def __init__(self, proxy: str | None = None, user_agent: str = "") -> None:
        self.proxy = proxy
        self.user_agent = user_agent

    def fetch(self, source: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
        fname = str(source.get("fname") or "").strip()
        if not fname:
            log.warning("源 %s 缺少 fname", source.get("id"))
            return None
        try:
            return asyncio.run(self._fetch_async(source, since, fname))
        except Exception as exc:  # noqa: BLE001
            log.warning("贴吧抓取失败 %s: %s", fname, str(exc)[:80])
            return None

    async def _fetch_async(
        self, source: dict[str, Any], since: datetime, fname: str
    ) -> list[dict[str, Any]]:
        import aiotieba as tb

        keywords = [str(k) for k in (source.get("keywords") or []) if k]
        limit = int(source.get("max_items", 30) or 30)

        # 按发布时间排序：情报要「新」不是「热」；一周窗口通常需要翻 2~3 页
        pages = max(1, int(source.get("pages", 3) or 3))
        objs: list[Any] = []
        seen_tid: set[str] = set()
        async with tb.Client() as client:
            for pn in range(1, pages + 1):
                res = await client.get_threads(
                    fname, pn=pn, rn=min(limit, 100), sort=tb.ThreadSortType.CREATE
                )
                err = getattr(res, "err", None)
                if err is not None:
                    log.warning("贴吧 %s 第 %d 页 err=%s", fname, pn, err)
                    break
                batch = getattr(res, "objs", None) or []
                if not batch:
                    break
                fresh = 0
                for th in batch:
                    tid = str(getattr(th, "tid", "") or "")
                    if tid and tid not in seen_tid:
                        seen_tid.add(tid)
                        objs.append(th)
                        fresh += 1
                # 本页全是被时间窗挡掉的旧帖，就没必要再翻
                if fresh == 0:
                    break

            out: list[dict[str, Any]] = []
            for t in objs[:limit]:
                title = (getattr(t, "title", "") or "").strip()
                tid = getattr(t, "tid", None)
                if not title or not tid:
                    continue
                if keywords and not any(k in title for k in keywords):
                    continue
                created = getattr(t, "create_time", None)
                try:
                    published = datetime.fromtimestamp(int(created), tz=CST)
                except (TypeError, ValueError, OSError):
                    published = None
                if published is not None and published < since:
                    continue
                tid_s = str(tid)
                out.append(
                    {
                        "source_id": source["id"],
                        "url": f"https://tieba.baidu.com/p/{tid_s}",
                        "title": title,
                        # 标题 + 回复数足够做判断；正文留给正文抓取阶段
                        "body": f"{title}（贴吧·{fname}，回复 {getattr(t, 'reply_num', 0)}）",
                        "lang": "zh",
                        "published_at": (
                            published.replace(microsecond=0).isoformat() if published else None
                        ),
                        "fetched_at": now_iso(),
                        "content_hash": _hash(title, tid_s),
                        "publisher_domain": "tieba.baidu.com",
                        "time_source": "article" if published else "fetch",
                        "engagement": int(getattr(t, "reply_num", 0) or 0),
                    }
                )
            log.info("贴吧 %s：%d 帖，命中 %d 条", fname, len(objs), len(out))
            return out


def _hash(title: str, tid: str) -> str:
    import hashlib
    import re

    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha1(f"{norm}|{tid}".encode("utf-8")).hexdigest()
