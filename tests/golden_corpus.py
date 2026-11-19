# -*- coding: utf-8 -*-
"""金标准语料：一组人工构造、结果已知的文档与 LLM 分析。

为什么要有它：聚类/评分这些高风险逻辑以前只验证「不崩」，所以「20 篇无关帖并成
一条」「同输入两次跑分数不同」这类问题一直没被发现。这里把「什么该合并、什么不该
合并」固化成断言，改动后跑一遍就知道有没有退化。

每个分组故意覆盖一类边界：
  same_event    同一事件不同措辞/不同源 → 必须合并成 1 条
  same_title    标题完全相同、来源不同 → 必须合并
  generic_only  只共享泛化实体（子版块/系列名）→ 必须不合并
  cjk_same      中文同一事件 → 必须合并
  undated       没有发布时间的文档 → 不得因此被丢弃
"""
from __future__ import annotations

from typing import Any

BASE = "2026-11-19"

# (组, id, 标题, 实体列表, published_at, source_id, publisher_domain)
ROWS: list[tuple[str, int, str, list[str], str | None, str, str]] = [
    # --- 同一事件，三家不同措辞：共享 3 个具体实体，标题有交集 → 合并
    ("same_event", 1, "New Battleforce boxes revealed for Warhammer 40,000",
     ["Warhammer 40,000", "Battleforce", "Blood Angels", "Space Marines"],
     "2026-11-18T09:00:00+08:00", "gnews_40k", "bell-of-lost-souls.com"),
    ("same_event", 2, "Sunday Preview – New Battleforce Boxes Muster for Action",
     ["Warhammer 40,000", "Battleforce", "Blood Angels", "Space Marines"],
     "2026-11-18T10:00:00+08:00", "whc_news", "warhammer-community.com"),
    ("same_event", 3, "Saturday Pre-order – Grab your Battleforce boxes today",
     ["Warhammer 40,000", "Battleforce", "Space Marines", "Blood Angels"],
     "2026-11-18T11:00:00+08:00", "goonhammer", "goonhammer.com"),

    # --- 标题完全相同、来源不同：必须合并
    ("same_title", 4, "Games Workshop Pre-Orders: Holiday Battleforce Boxes are Here",
     ["Games Workshop", "Battleforce"], "2026-11-18T12:00:00+08:00",
     "gnews_aos", "spikeybits.com"),
    ("same_title", 5, "Games Workshop Pre-Orders: Holiday Battleforce Boxes are Here",
     ["Games Workshop", "Battleforce"], "2026-11-18T12:30:00+08:00",
     "reddit_40k", "reddit.com"),

    # --- 只共享泛化实体（子版块名 + 系列名）：标题毫无关系 → 必须保持独立
    ("generic_only", 6, "Tilea: Finalizing Luka's Legendary Lions",
     ["Warhammer Fantasy", "Reddit r/WarhammerFantasy"],
     "2026-11-18T13:00:00+08:00", "reddit_tow", "reddit.com"),
    ("generic_only", 7, "Is the Old World TTRPG considered a new edition of Fantasy?",
     ["Warhammer Fantasy", "Reddit r/WarhammerFantasy"],
     "2026-11-18T13:10:00+08:00", "reddit_tow", "reddit.com"),
    ("generic_only", 8, "Girl power! The Daughters of Averlion are here!",
     ["Warhammer Fantasy", "Reddit r/WarhammerFantasy"],
     "2026-11-18T13:20:00+08:00", "reddit_tow", "reddit.com"),
    ("generic_only", 9, "The Battle of Osterwald - Armies and Introduction",
     ["Warhammer Fantasy", "Reddit r/WarhammerFantasy"],
     "2026-11-18T13:30:00+08:00", "reddit_tow", "reddit.com"),

    # --- 中文同一事件：标题措辞不同，共享 2 个具体实体 → 合并
    ("cjk_same", 10, "《战锤40K：暗潮》修女见习者职业公布",
     ["战锤40K", "暗潮", "修女见习者"], "2026-11-18T14:00:00+08:00",
     "bilibili_40k", "bilibili.com"),
    ("cjk_same", 11, "战锤40K暗潮 修女见习者 玩法解析",
     ["战锤40K", "暗潮", "修女见习者"], "2026-11-18T14:10:00+08:00",
     "bilibili_wh", "bilibili.com"),

    # --- 没有发布时间：不得被丢弃，也不得因此与别人合并
    ("undated", 12, "Hobby Build Academy: Holiday Gift Advice",
     ["Hobby Build Academy"], None, "whc_news", "warhammer-community.com"),

    # --- 另外两条无关内容，确保簇数稳定
    ("misc", 13, "Golden Demon 2026 – SPIEL Essen Finalists, Part One",
     ["Golden Demon"], "2026-11-18T15:00:00+08:00", "whc_news", "warhammer-community.com"),
    ("misc", 14, "Golden Demon 2026 – SPIEL Essen Finalists, Part Two",
     ["Golden Demon"], "2026-11-18T15:10:00+08:00", "whc_news", "warhammer-community.com"),
]

# 组 → 期望的 story 数（写死，改动聚类逻辑必须同步更新并说明理由）
EXPECTED_STORIES = {
    "same_event": 1,
    "same_title": 1,
    "generic_only": 4,
    "cjk_same": 1,
    "undated": 1,
    "misc": 1,
}


def build() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """返回 (docs, analyses)，id 与列表下标一致。"""
    docs: list[dict[str, Any]] = []
    analyses: dict[int, dict[str, Any]] = {}
    for i, (group, rid, title, ents, published, source, domain) in enumerate(ROWS):
        docs.append({
            "id": rid,
            "group": group,
            "title": title,
            "url": f"https://example.com/{rid}",
            "body": "x" * 300,
            "lang": "zh" if group == "cjk_same" else "en",
            "published_at": published,
            "fetched_at": f"{BASE}T16:00:{i:02d}+08:00",
            "discovered_at": f"{BASE}T16:00:{i:02d}+08:00",
            "publisher_domain": domain,
            "source_id": source,
            "tier": "L1" if domain in ("warhammer-community.com",) else "L2",
        })
        analyses[i] = {
            "title_zh": "",
            "summary": f"摘要 {rid}",
            "why_matters": "",
            "event_type": "rules",
            "entities": ents,
            "category": "40k",
            "impact_scope": "global",
            "magnitude": 3.0,
            "actionability": 3.0,
            "is_rumor": False,
            "claims": [],
            "quote": "",
        }
    return docs, analyses
