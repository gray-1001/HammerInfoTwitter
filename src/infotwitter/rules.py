"""规则引擎：无 LLM 时的降级抽取。

LLM 不可用（没配 key、预算耗尽、调用失败）时，用关键词规则给出
事件类型、品类、量级、可行动性的近似判断。翻译与摘要留空，
但排序仍然成立——这保证系统在零成本下也是可用的，而不是空日报。

规则刻意保守：命中即停，宁可判成 other 也不乱猜。
"""
from __future__ import annotations

from typing import Any

# (关键词, 事件类型, 量级 0-5, 可行动性 0-5)
EVENT_RULES: list[tuple[tuple[str, ...], str, float, float]] = [
    # 财报/公司动态权重最高：影响整个产品线
    (("revenue", "financial results", "half-year", "share price", "investor",
      "annual report", "profit", "股价", "财报"), "business", 4.5, 1.0),
    # 新版本/新游戏立项
    (("new edition", "edition launch", "announced", "reveals", "unveils",
      "新版本", "公布", "官宣"), "rules", 4.0, 3.0),
    (("codex", "battletome", "army book", "battleforce", "战役书"), "rules", 3.5, 3.5),
    (("balance", "dataslate", "errata", "faq", "points update", "metawatch",
      "平衡", "补丁"), "rules", 3.0, 3.0),
    # 新品
    (("pre-order", "preorder", "pre order", "sunday preview", "saturday pre-order",
      "new model", "new kit", "made to order", "last chance",
      "预售", "开订", "新品", "上新"), "mini", 3.0, 4.0),
    # 出版
    (("novel", "black library", "anthology", "audiobook", "hardback", "paperback",
      "小说", "出版", "引进"), "novel", 2.5, 3.0),
    # 电子游戏
    (("video game", "dlc", "trailer", "steam", "playstation", "xbox",
      "survivors", "dawn of war", "space marine 2", "darktide", "total war",
      "游戏", "预告片"), "game", 3.0, 4.0),
    # 活动赛事
    (("golden demon", "tournament", "championship", "convention", "grand narrative",
      "赛事", "展会", "门店", "俱乐部"), "event", 2.0, 2.0),
]

CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("horus heresy", "30k", "legions imperialis", "heresy", "荷鲁斯"), "30k"),
    (("age of sigmar", "sigmar", "stormcast", "skaven", "soulblight", "khazalite",
      "西格玛", "中古西格玛"), "aos"),
    (("old world", "tomb kings", "bretonnia", "khemri", "arcane journal", "cathay",
      "中古", "旧世界"), "tow"),
    (("40k", "40,000", "space marine", "imperium", "adeptus", "astartes", "chaos",
      "星际战士", "帝国", "混沌"), "40k"),
]

RUMOR_MARKERS = (
    "rumour", "rumor", "leak", "leaked", "speculation", "unconfirmed",
    "传闻", "爆料", "泄露", "疑似",
)

# 常青内容：教程、榜单、百科式科普。长期有效但"不是新闻"，不该占日报槽位。
EVERGREEN_PATTERNS = (
    "explained", "guide", "how to", "beginner", "getting started",
    "everything you need", "tier list", "top 10", "top 5", "best ",
)

# 中文社区专属信号：影响面只在中文圈
CN_MARKERS = (
    "中文本", "中文版", "引进", "简体中文", "国内", "中国区", "上海", "北京",
    "淘宝", "闲鱼", "代购", "汉化", "锤店", "俱乐部",
)


def classify(title: str, body: str, lang: str = "en") -> dict[str, Any]:
    """返回规则判断结果（不含翻译/摘要）。"""
    blob = f"{title or ''} {body or ''}".lower()

    event_type, magnitude, actionability = "other", 1.5, 1.0
    for keywords, etype, mag, act in EVENT_RULES:
        if any(k in blob for k in keywords):
            event_type, magnitude, actionability = etype, mag, act
            break

    category = "other"
    for keywords, cat in CATEGORY_RULES:
        if any(k in blob for k in keywords):
            category = cat
            break

    is_rumor = any(k in blob for k in RUMOR_MARKERS)

    # 常青内容降权：教程/榜单/百科不是新闻，压到最低量级
    title_blob = (title or "").lower()
    evergreen = any(p in title_blob for p in EVERGREEN_PATTERNS)
    if evergreen and event_type in ("other", "novel", "game"):
        magnitude = min(magnitude, 1.0)
        actionability = min(actionability, 1.0)

    # 中文来源默认视为中文社区影响面（避免与英文原文在通识槽里重复）；
    # 英文内容若明确涉及中国区，也归入中文社区。
    if lang == "zh" or any(k in blob for k in CN_MARKERS):
        impact_scope = "cn_community"
    else:
        impact_scope = "global"

    return {
        "event_type": event_type,
        "category": category,
        "magnitude": magnitude,
        "actionability": actionability,
        "is_rumor": is_rumor,
        "impact_scope": impact_scope,
    }


def describe(event_type: str) -> str:
    return {
        "business": "公司/财报动态",
        "rules": "规则与版本更新",
        "mini": "模型新品与预售",
        "novel": "小说与出版",
        "game": "电子游戏动态",
        "event": "赛事与活动",
        "other": "综合信息",
    }.get(event_type, "综合信息")
