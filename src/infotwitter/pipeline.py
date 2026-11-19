"""流水线核心：归一化 → 聚类 → 评分 → 槽位分配。"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from .config import Config
from .db import CST, parse_dt
from .llm import fallback_analysis

log = logging.getLogger(__name__)

PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff\s]")
SPACE_RE = re.compile(r"\s+")

TIER_SCORE = {"L0": 5.0, "L1": 4.0, "L2": 2.5, "L3": 1.0}
TIER_ORDER = ["L3", "L2", "L1", "L0"]

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
    "new", "warhammer", "40k", "40000", "40,000", "age", "sigmar", "part",
}

# 泛化实体：几乎每篇战锤文章都会提到，用它们判断「同一事件」会导致灾难性合并。
# 实测未过滤时 25 篇无关文章被并成一个簇（战争黎明、Khemri、AoS 全被吞掉）。
GENERIC_ENTITIES = {
    "warhammer", "warhammer 40k", "warhammer 40,000", "40k", "战锤", "战锤40k",
    "warhammer community", "games workshop", "gw", "warhammer age of sigmar",
    "age of sigmar", "the old world", "warhammer: the old world", "sigmar",
    "space marines", "space marine", "太空陆战队", "星际战士",
    "reddit", "reddit r/warhammer40k", "reddit r/warhammer", "b站", "bilibili",
    "goonhammer", "spikey bits", "bell of lost souls", "wargamer", "youtube",
    "auspex tactics", "warhammer tv", "black library",
}

MIN_SHARED_ENTITIES = 2  # 需要共享的「具体」实体数

# ---------------------------------------------------------------- 合并约束
# 这些是「约束」，不是「特例」：以前靠往 GENERIC_ENTITIES 里补词来防误合并，
# 补不完（漏了 warhammer fantasy、系列名、媒体名），而且补一个漏一个。
# 现在的做法是给合并加三个必须同时满足的条件，任何一条不满足就不合并。
#
# 1) 实体共享只是必要条件，标题还必须有交集：
#    同一个子版块（r/warhammerfantasy）的 20 篇闲聊共享 2 个实体，但标题毫无关系。
ENTITY_MERGE_MIN_JACCARD = 0.08
# 2) 子集关系（标题 A 是 B 的前缀）不能单独作为合并依据，还需相似度够高，
#    否则「XX 预告」会被并进「XX 预告 + 全部相关报道」。
SUBSET_MERGE_MIN_JACCARD = 0.40
# 3) 簇级内聚度：成员两两平均相似度低于此值就拆开。
#    并查集是传递的，A-B、B-C 成立就会把 A/C 并一起；只校验「对」不够，必须校验「簇」。
CLUSTER_COHESION_MIN = 0.20
CLUSTER_COHESION_MIN_SIZE = 8  # 只对大簇做内聚度校验：2-5 篇的小簇通常是真·多源报道
# 4) 数据驱动的泛化实体：出现在 >15% 文档里的实体不参与合并（样本 >=30 才有统计意义）。
#    这是「从语料算出来的约束」，新出现的泛词不用再手工加黑名单。
GENERIC_ENTITY_DF_RATIO = 0.15
GENERIC_ENTITY_MIN_DOCS = 30
# 聚合器/阅读器域名：它们只证明"被收录"，不能当作独立信源计数（S-02）
AGGREGATOR_DOMAINS = {
    "news.google.com",
    "feedproxy.google.com",
    "flipboard.com",
    "news.ycombinator.com",
}
# 补位事件的每日衰减系数。aged 与 backlog 必须用同一把尺子，
# 否则同一批候选在两级阶梯里会算出不同的排序（N-12）。
DECAY_PER_DAY = 0.92

# 槽 5 只收「商品信息」：新品、预售、复刻、降价。
# 设计 §9.1 的 SKU 范围涵盖 boxed / codex / mini / book / paint，
# 所以不能只认 event_type=mini（核查报告 BUG-28）。
PRODUCT_HINTS = (
    "预售", "预购", "开订", "发售", "上市", "新品", "上新", "复刻", "降价", "折扣",
    "pre-order", "preorder", "pre order", "new model", "new kit", "new box",
    "revealed", "made to order", "battleforce", "release",
)

# 强商品信号：即便模型把事件判成 other，只要命中这些词也认为是商品信息
PRODUCT_STRONG_HINTS = (
    "预售", "预购", "开订", "pre-order", "preorder", "made to order",
    "battleforce", "new kit", "new model", "复刻", "降价", "发售",
)

# 反向信号：涂装分享、评测、战报、教程都不是「商品快讯」
PRODUCT_NEGATIVE_HINTS = (
    "涂装", "教程", "评测", "战报", "同人", "梗图", "盲盒开箱",
    "review", "how to", "guide", "tutorial", "battle report", "painting",
)

PRODUCT_EVENT_TYPES = ("mini", "novel", "rules")


def is_product_item(story: dict[str, Any]) -> bool:
    """槽 5 的入选判定。

    设计 §9.1 的商品范围是 boxed/codex/mini/book/paint，比 "mini" 更宽，
    因此改为「事件类型 + 强商品词 + 反向词排除 + 可行动性」四条件。
    """
    blob = f"{story.get('title_zh') or ''} {story.get('canonical_title') or ''}".lower()
    if any(bad in blob for bad in PRODUCT_NEGATIVE_HINTS):
        return False
    if float(story.get("actionability_raw") or 0) < 3:
        return False
    if story.get("event_type") in PRODUCT_EVENT_TYPES and any(h in blob for h in PRODUCT_HINTS):
        return True
    return any(h in blob for h in PRODUCT_STRONG_HINTS)


def normalize_title(title: str) -> str:
    text = (title or "").lower()
    text = PUNCT_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def token_set(title: str) -> set[str]:
    """标题词集。

    CJK 里两字词很常见（「分家」「战锤」），原来 `len > 2` 会把它们全丢掉，
    词集为空 → 两篇同标题文档永远不聚成一簇，story 身份也就无从复用（P1-4）。
    """
    norm = normalize_title(title)
    tokens = {
        t for t in norm.split()
        if t not in STOPWORDS and (len(t) > 2 or _is_cjk_token(t))
    }
    # 中文标题常常整段没有空格，只按空格切会得到「战锤40k暗潮」「修女见习者职业公布」
    # 这种整块 token，两篇讲同一件事的标题交集为 0（实测 Jaccard=0.0）。
    # 只要有汉字就补字符二元组，让中文也能按「词」比较，而不是只在全空时兜底。
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", norm))
    if len(cjk) >= 2:
        tokens |= {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    if tokens:
        return tokens
    # 全是短词/无空格的中文标题：退回字符二元组，保证同一标题的指纹稳定
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", norm)
    return {cjk[i:i + 2] for i in range(len(cjk) - 1)}


def _is_cjk_token(token: str) -> bool:
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,}", token or ""))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def story_id_for(title: str, first_seen: str) -> str:
    key = f"{normalize_title(title)[:80]}|{first_seen[:10]}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


_TIER_BUZZ_WEIGHT = {"L0": 1.0, "L1": 1.0, "L2": 0.6, "L3": 0.3}
_TIER_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}

# 二级后缀：取"注册域"时不能只砍两级，否则 co.uk 这类会算错
_MULTI_SUFFIX = ("co.uk", "com.au", "co.jp", "com.cn", "co.nz", "com.br",
                 "co.kr", "com.tw", "co.in", "com.hk", "com.sg")


def registrable_domain(host: str) -> str:
    """取注册域：blogs.nvidia.com → nvidia.com。

    同一家厂商的多个子域（nvidia.com / blogs.nvidia.com）不是两个独立信源，
    以前会把一份厂商通稿算成"两家在报"（N-2）。
    """
    host = re.sub(r"^www\.", "", str(host or "").strip().lower())
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _MULTI_SUFFIX:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _source_domain(d: dict[str, Any]) -> str:
    """取用于计数的注册域；聚合器与空值返回空串。"""
    raw = str(d.get("publisher_domain") or "")
    if not raw:
        raw = urlparse(str(d.get("url") or "")).netloc
    raw = re.sub(r"^www\.", "", str(raw).strip().lower())
    if not raw:
        return ""
    # 聚合器判断要在"折叠子域"之前：news.google.com 折叠成 google.com 之后
    # 就不在名单里了，会把聚合器当成真实媒体算一个来源（回归测试抓到过）
    if raw in AGGREGATOR_DOMAINS or raw.endswith(".google.com"):
        return ""
    dom = registrable_domain(raw)
    if not dom or dom in AGGREGATOR_DOMAINS:
        return ""
    return dom


def independent_weight(docs: list[dict[str, Any]]) -> float:
    """按「来源类型」加权的独立来源数。

    转载型媒体（L2）与原创/官方（L0/L1）不能等权：一份厂商通稿被三家科技站
    转一圈，不该等于三家独立媒体在互相印证（N-2）。
    """
    best: dict[str, str] = {}
    for d in docs:
        dom = _source_domain(d)
        if not dom:
            continue
        tier = str(d.get("tier") or "L2")
        if dom not in best or _TIER_RANK.get(tier, 2) < _TIER_RANK.get(best[dom], 2):
            best[dom] = tier
    return sum(_TIER_BUZZ_WEIGHT.get(t, 0.6) for t in best.values())


def independent_sources(docs: list[dict[str, Any]]) -> set[str]:
    """独立来源 = 不同的发布注册域。

    不能拿 source_id 充当「独立信源」：同一个 B 站视频会被 bilibili_wh /
    bilibili_model / bilibili_40k 三个采集配置各收录一次；同一个 Google News
    条目也会被多个查询命中。按 source_id 计数会把「自己和自己」算成多源印证
    （核查报告 BUG-03）。

    聚合器域名要排除：Google News 的条目在拿不到 `<source>` 时，URL 是
    news.google.com，两条不同媒体的报道会被算成同一个域名（真多源无法升级）；
    反过来，一条真实 RSS + 一条同域名的 gnews 条目又会被算成两个域名（虚假升级）。
    宁可不算，也不能算错（S-02）。

    同一厂商的子域合并成一个（blogs.nvidia.com == nvidia.com），否则一份官方
    通稿自带两个域名就能冒充"多源"（N-2）。
    """
    return {dom for d in docs if (dom := _source_domain(d))}


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------- 聚类
def _split_incohesive(members: list[int], tokens: list[set[str]]) -> list[list[int]]:
    """把「靠传递闭包滚出来」的大簇按标题相似度重切。

    只校验「两篇能不能合并」是不够的：并查集会把 A-B、B-C 链成 A/C。
    这里对簇整体做一次内聚度校验，低于阈值就用确定性的贪心重切
    （成员按 index 排序、取第一个够相似的桶），保证同输入同输出。
    """
    if len(members) < CLUSTER_COHESION_MIN_SIZE:
        return [sorted(members)]
    ordered = sorted(members)
    pairs = [(a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:]]
    mean = sum(jaccard(tokens[a], tokens[b]) for a, b in pairs) / len(pairs)
    if mean >= CLUSTER_COHESION_MIN:
        return [ordered]
    buckets: list[list[int]] = []
    for idx in ordered:
        for bucket in buckets:
            if any(jaccard(tokens[idx], tokens[m]) >= CLUSTER_COHESION_MIN for m in bucket):
                bucket.append(idx)
                break
        else:
            buckets.append([idx])
    return buckets


def cluster_docs(
    docs: list[dict[str, Any]],
    analyses: dict[int, dict[str, Any]],
    doc_to_story: dict[int, str] | None = None,
    story_first_seen: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """把原始文档按标题相似度聚成 story。

    doc_to_story / story_first_seen 传入时，会沿「文档归属链」复用已有 story_id，
    而不是每次都按「簇内最早那篇」重新算（A-06）。
    """
    n = len(docs)
    if n == 0:
        return []

    tokens = [token_set(d.get("title") or "") for d in docs]

    def _specific(raw: Any) -> set[str]:
        return {
            str(e).strip().lower()
            for e in (raw or [])
            if e and str(e).strip().lower() not in GENERIC_ENTITIES
        }

    entity_sets = [
        _specific((analyses.get(i) or {}).get("entities")) for i in range(n)
    ]
    # 动态泛化实体：统计每个实体出现在多少篇里，超过比例就判为泛化。
    # 只在样本足够时启用，否则小批量会把真的关键实体误判成泛化。
    if n >= GENERIC_ENTITY_MIN_DOCS:
        df: dict[str, int] = {}
        for es in entity_sets:
            for e in es:
                df[e] = df.get(e, 0) + 1
        too_common = {e for e, c in df.items() if c / n > GENERIC_ENTITY_DF_RATIO}
        if too_common:
            entity_sets = [es - too_common for es in entity_sets]

    uf = UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if uf.find(i) == uf.find(j):
                continue
            sim = jaccard(tokens[i], tokens[j])
            shared = len(entity_sets[i] & entity_sets[j])
            # 约束 1：共享实体 + 标题有交集，两个条件同时成立才算同一事件
            if shared >= MIN_SHARED_ENTITIES and sim >= ENTITY_MERGE_MIN_JACCARD:
                uf.union(i, j)
            elif sim >= 0.5:
                uf.union(i, j)
            # 约束 2：子集关系需要更高的相似度兜底
            elif tokens[i] and tokens[j] and (tokens[i] <= tokens[j] or tokens[j] <= tokens[i]):
                if min(len(tokens[i]), len(tokens[j])) >= 4 and sim >= SUBSET_MERGE_MIN_JACCARD:
                    uf.union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    # 约束 3：大簇内聚度不足就拆。并查集只保证「连得上」，不保证「是一件事」。
    members_list: list[list[int]] = []
    for members in groups.values():
        members_list.extend(_split_incohesive(members, tokens))
    groups = {idx: m for idx, m in enumerate(members_list)}

    stories: list[dict[str, Any]] = []
    for members in groups.values():
        # 文档与分析必须成对传入：只排文档会让「标题取最早、摘要取最新」（BUG-12）
        story = _build_story([(docs[i], analyses.get(i)) for i in members])
        if story:
            stories.append(story)

    if doc_to_story:
        assign_stable_ids(stories, doc_to_story, story_first_seen or {})
    return stories


def assign_stable_ids(
    stories: list[dict[str, Any]],
    doc_to_story: dict[int, str],
    story_first_seen: dict[str, str],
) -> None:
    """按「文档归属链」给本轮事件分配 story_id，尽量复用已有 id。

    规则：
    - 一个簇的成员文档若历史上属于某个 story，就沿用那个 id（按 first_seen 取最老的；
      合并时老事件胜出）；
    - 拆分时多个簇会抢同一个 id：先按 first_seen 排序，先到的占用，后来的重新生成；
    - 没有任何历史归属的簇，用原本从标题算出来的 id。
    """
    used: set[str] = set()

    def _order_key(s: dict[str, Any]) -> tuple:
        # 老事件优先占用 id，保证合并后存活的是历史更久的那条。
        # 同一时间戳的多个簇必须再按「最小成员文档 id」排序：否则谁先拿到
        # 未加盐的基础 id 取决于输入顺序，结果仍不可复现（salt 确定也没用）。
        members = sorted(int(m) for m in (s.get("member_ids") or []) if str(m).isdigit())
        return (
            str(s.get("first_seen_at") or ""),
            members[0] if members else 0,
            str(s.get("canonical_title") or ""),
        )

    ordered = sorted(stories, key=_order_key)
    for story in ordered:
        candidates = {
            doc_to_story[d]
            for d in story.get("member_ids") or []
            if d in doc_to_story
        }
        chosen = ""
        # 候选 id 按 (first_seen, id) 排序：只按 first_seen 时同秒的多个候选
        # 会随 set 迭代顺序变化，导致同一事件每天换 id（P1-4）
        for cand in sorted(candidates, key=lambda x: (str(story_first_seen.get(x) or "9999"), x)):
            if cand not in used:
                chosen = cand
                break
        if not chosen:
            chosen = str(story["id"])
            if chosen in used:
                # 同一轮里另一个簇已经用了这个 id（拆分场景），另起一个。
                # salt 必须由「簇成员」决定，不能是 len(used)：
                # 后者依赖遍历顺序，同样的输入换个顺序就得到不同 id，
                # 于是每轮重跑都新造一条 story，旧的不清理就变成孤儿
                # （实测同一 B 站视频产生 4 条 story，3 条无文档归属）。
                members = sorted(story.get("member_ids") or [])
                salt = hashlib.sha1(",".join(str(m) for m in members).encode()).hexdigest()[:8]
                chosen = story_id_for(f"{story.get('canonical_title', '')}#{salt}",
                                      str(story.get("first_seen_at") or ""))
        story["id"] = chosen
        used.add(chosen)


def _build_story(pairs: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> dict[str, Any] | None:
    if not pairs:
        return None
    # 成对按发布时间升序：primary = 最早那篇，保证 canonical_title / story_id 稳定。
    # 同一秒并列时必须补一个稳定键：只按 published 排序会退化为输入顺序，
    # 同一批数据换个顺序就会算出不同的分数与标题（实测 15/564 条分数漂移）。
    pairs = sorted(
        pairs,
        key=lambda p: (
            p[0].get("published_at") or p[0].get("fetched_at") or "",
            str(p[0].get("id") or p[0].get("url") or ""),
        ),
    )
    docs = [p[0] for p in pairs]
    primary = docs[0]

    picked: dict[str, Any] | None = None
    picked_doc: dict[str, Any] = primary
    for d, a in pairs:
        if a and not a.get("fallback"):
            picked, picked_doc = a, d
            break
    if picked is None:
        first_doc, first_analysis = pairs[0]
        if first_analysis:
            picked, picked_doc = first_analysis, first_doc
        else:
            picked = fallback_analysis(primary)

    tiers = [d.get("tier") or "L2" for d in docs]
    best = max(tiers, key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else 0)
    distinct_sources = independent_sources(docs)
    authenticity = best
    note = f"来源 {len(distinct_sources)} 个，最高层级 {best}"
    if len(distinct_sources) >= 2 and best == "L2":
        # 仅 L2 可因多源印证升级；L3 不升级，防同源洗稿（见设计 §6.3）
        authenticity = "L1"
        note += f"，多源印证升级为 {authenticity}"
    if picked.get("is_rumor") and authenticity != "L0":
        # 传闻措辞一律按 L3 处理，不占前 4 槽位；官方源的爆料专栏除外
        authenticity = "L3"
        note += "，传闻措辞降级为 L3"

    # published 未知时保持 None，**不要**回退成抓取时间——回退等于伪造"刚刚发布"，
    # 会让 24h 推送窗口与时效计分同时失效（C-01）
    published = primary.get("published_at")
    time_source = primary.get("time_source") or "feed"
    # 「首次出现」= 首次入库时间，而不是原文发布时间：延迟发布/延迟发现的新闻
    # 不应因为 published_at 早就被 24h 推送窗口丢掉（BUG-05）
    first_seen = min(
        (d.get("discovered_at") or d.get("published_at") or d.get("fetched_at") or "" for d in docs),
        default="",
    ) or published
    title_zh = picked.get("title_zh") or ""
    # story_id 必须只依赖原文标题 + 首发日期：若用 LLM 译名或入库时间，ID 会漂移，
    # 台账去重会失效并导致同一事件被重复推送。
    stable_title = primary.get("title") or ""

    # 优先使用真实原文链接，避免把 Google News 跳转地址当作原文
    real = next(
        (d for d in docs if d.get("url") and "news.google.com" not in d["url"]),
        None,
    )
    primary_url = (real or primary).get("url")

    raw_doc_id = picked_doc.get("id")
    # §6.3：claims 跨文档聚合（这才叫交叉印证），同时做矛盾检测
    claims, has_conflict = aggregate_claims(pairs)
    if not claims:
        # 老数据（本功能上线前的 analysis）里没有 claims，退回 picked 的单条主张
        claims = [
            {**c, "raw_doc_id": raw_doc_id, "support_count": 1,
             "promoted": False, "contradict": 0}
            for c in picked.get("claims") or []
            if isinstance(c, dict)
        ]

    if has_conflict and authenticity != "L2":
        # 矛盾 → 降一级并加注（设计 §6.3）
        idx = TIER_ORDER.index(authenticity) if authenticity in TIER_ORDER else 1
        authenticity = TIER_ORDER[max(1, idx - 1)]
        note += "，存在相互矛盾的 claim，降级处理"

    quote_zh = str(picked.get("quote_zh") or "")
    confidence = confidence_text(authenticity, len(distinct_sources), note, has_conflict)

    return {
        "id": story_id_for(stable_title, published or first_seen),
        "canonical_title": primary.get("title") or stable_title,
        "title_zh": title_zh,
        "event_type": picked.get("event_type", "other"),
        "category": picked.get("category", "other"),
        "impact_scope": picked.get("impact_scope", "global"),
        "entities": picked.get("entities") or [],
        "first_seen_at": first_seen,
        "published_at": published,
        "time_source": time_source,
        "last_seen_at": docs[-1].get("fetched_at") or first_seen,
        "source_ids": sorted({d["source_id"] for d in docs}),
        "authenticity": authenticity,
        "authenticity_note": note,
        "confidence": confidence,
        "summary": picked.get("summary", ""),
        "why_matters": picked.get("why_matters", ""),
        "quote_en": picked.get("quote_en", ""),
        "quote_zh": quote_zh,
        "primary_url": primary_url,
        "claims": claims,
        "claim_conflict": 1 if has_conflict else 0,
        "member_ids": [d["id"] for d in docs if d.get("id") is not None],
        "magnitude_raw": picked.get("magnitude", 2.0),
        "actionability_raw": picked.get("actionability", 1.0),
        "n_sources": len(distinct_sources),
        "buzz_weight": round(independent_weight(docs), 2),
        "status": "new",
    }


# ---------------------------------------------------------------- claim 聚合（§6.3）
CLAIM_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
CLAIM_CONFLICT_TYPES = ("price", "date")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def normalize_claim(text: str) -> str:
    """把 claim 归一成「去掉数字与标点」的骨架，用于跨文档比对同一条主张。"""
    text = normalize_title(text)
    return CLAIM_NUMBER_RE.sub("#", text).strip()


def claim_numbers(text: str) -> tuple[str, ...]:
    return tuple(sorted(CLAIM_NUMBER_RE.findall(text or "")))


def claim_tokens(text: str) -> set[str]:
    """claim 的比对单元。

    中文没有空格，按空白切词会把整句当成一个 token，导致中英混排时
    交叉印证永远匹配不上。这里对 CJK 用二元字符组，对拉丁文用词。
    """
    norm = normalize_claim(text)
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", norm)
    tokens = token_set(norm)
    if cjk:
        tokens |= {cjk[i:i + 2] for i in range(max(1, len(cjk) - 1))}
    return tokens


def aggregate_claims(
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]],
    sim_min: float = 0.5,
) -> tuple[list[dict[str, Any]], bool]:
    """跨文档聚合 claim，实现设计 §6.3 的交叉印证与矛盾检测。

    每个文档是**独立**送进 LLM 的，同一件事的表述不会逐字相同，
    所以不能只按字符串相等归并——这里用「去数字后的词集合 Jaccard ≥ 0.5」做模糊归并，
    否则交叉印证永远是 0 条（实测：精确归并命中 0/408）。

    - 同一主张被 ≥2 个**不同域名**支持，且其中至少一个来源是 L0/L1 → 标记 promoted；
    - 同一主张骨架下出现不同的价格/日期数字 → 判定矛盾；
    - L3 来源之间的相互支持不计入（防同源洗稿）。

    返回 (claims, has_conflict)。
    """
    buckets: list[dict[str, Any]] = []

    def _match(tokens: set[str]) -> dict[str, Any] | None:
        best, best_sim = None, 0.0
        for b in buckets:
            sim = jaccard(tokens, b["tokens"])
            if sim > best_sim:
                best, best_sim = b, sim
        return best if best_sim >= sim_min else None

    for doc, analysis in pairs:
        if not analysis:
            continue
        for claim in analysis.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "").strip()
            if not text:
                continue
            tokens = claim_tokens(text)
            if not tokens:
                continue
            domain = next(iter(independent_sources([doc])), str(doc.get("source_id") or ""))
            item = {
                "text": text,
                "type": str(claim.get("type") or "statement"),
                "evidence": str(claim.get("evidence") or ""),
                "raw_doc_id": doc.get("id"),
                "tier": str(doc.get("tier") or "L2"),
                "domain": domain,
            }
            bucket = _match(tokens)
            if bucket is None:
                buckets.append({"tokens": tokens, "items": [item]})
            else:
                bucket["items"].append(item)

    claims: list[dict[str, Any]] = []
    has_conflict = False
    for bucket in buckets:
        items = bucket["items"]
        domains = {it["domain"] for it in items}
        tiers = {it["tier"] for it in items}
        promoted = len(domains) >= 2 and bool(tiers & {"L0", "L1"})

        contradict = False
        if items[0]["type"] in CLAIM_CONFLICT_TYPES and len(items) > 1:
            numbers = {claim_numbers(it["text"]) for it in items}
            numbers.discard(())
            if len(numbers) > 1:
                contradict = True
                has_conflict = True

        rep = next((it for it in items if it["evidence"]), items[0])
        claims.append(
            {
                "text": rep["text"],
                "type": rep["type"],
                "evidence": rep["evidence"],
                "raw_doc_id": rep["raw_doc_id"],
                "support_count": max(1, len(domains)),
                "promoted": promoted,
                "contradict": 1 if contradict else 0,
            }
        )

    # promoted 的排前面，方便报告页展示「被印证」的主张
    claims.sort(key=lambda c: (not c["promoted"], -c["support_count"]))
    return claims, has_conflict


def confidence_text(auth: str, n_sources: int, note: str, conflict: bool) -> str:
    """可信度徽章文案（设计 §2.1 的 confidence 字段）。"""
    if conflict:
        return "主张存在矛盾"
    if auth == "L0":
        return "官方一手"
    if auth == "L1":
        return "多源印证" if "多源印证" in (note or "") else "权威报道"
    if auth == "L3":
        return "未证实"
    return "单一来源" if n_sources <= 1 else "社区多源"


# ---------------------------------------------------------------- 评分
def freshness_score(first_seen: str, now: datetime | None = None) -> float:
    dt = parse_dt(first_seen)
    if not dt:
        return 1.0
    now = now or datetime.now(CST)
    hours = max(0.0, (now - dt).total_seconds() / 3600)
    if hours <= 12:
        return 5.0
    if hours <= 24:
        return 4.0
    if hours <= 36:
        return 3.0
    if hours <= 48:
        return 2.5
    if hours <= 72:
        return 1.5
    if hours <= 120:
        return 1.0
    if hours <= 168:
        return 0.6
    return 0.2


def buzz_score(n_sources: int, weight: float | None = None) -> float:
    """传播热度。weight 是「按来源类型加权的独立来源数」，缺省退回原始条数。

    权重让转载型媒体（L2）只算 0.6：一份通稿被转一圈不等于三家独立媒体在印证。
    """
    n = float(weight) if weight is not None else float(n_sources)
    return min(5.0, 1.0 + 0.9 * max(0.0, n - 1))


def interest_score(story: dict[str, Any], interests: dict[str, float]) -> float:
    base = 2.5
    keys = [f"category:{story.get('category')}", f"type:{story.get('event_type')}"]
    keys += [f"entity:{e.lower()}" for e in (story.get("entities") or [])[:4]]
    hits = [interests[k] for k in keys if k in interests]
    if not hits:
        return base
    return max(0.0, min(5.0, base + 2.0 * (sum(hits) / len(hits))))


def score_story(story: dict[str, Any], cfg: Config, interests: dict[str, float],
                now: datetime | None = None) -> dict[str, Any]:
    weights = cfg.get("scoring.weights", {}) or {}
    w_auth = float(weights.get("authority", 20))
    w_mag = float(weights.get("magnitude", 25))
    w_buzz = float(weights.get("buzz", 20))
    w_act = float(weights.get("actionability", 15))
    w_fresh = float(weights.get("freshness", 10))
    w_int = float(weights.get("interest", 10))

    s_auth = TIER_SCORE.get(story.get("authenticity", "L2"), 2.5)
    s_mag = float(story.get("magnitude_raw", 2.0))
    s_buzz = buzz_score(int(story.get("n_sources", 1)), story.get("buzz_weight"))
    s_act = float(story.get("actionability_raw", 1.0))
    # 时效看「新闻有多新」（发布时间）；推送窗口看「我们多晚才发现」（首次入库）
    s_fresh = freshness_score(story.get("published_at") or story.get("first_seen_at") or "", now)
    if story.get("time_source") == "fetch":
        # 只能取到抓取时间的源（浏览器列表页没有日期），不许拿满分时效
        s_fresh = min(s_fresh, 3.0)
    s_int = interest_score(story, interests)

    raw = (
        w_auth * s_auth / 5
        + w_mag * s_mag / 5
        + w_buzz * s_buzz / 5
        + w_act * s_act / 5
        + w_fresh * s_fresh / 5
        + w_int * s_int / 5
    )

    cat_weights = cfg.get("scoring.category_weights", {}) or {}
    cat_w = float(cat_weights.get(story.get("category", "other"), 0.6))
    total = round(raw * cat_w, 1)

    breakdown = {
        "authority": round(s_auth, 2),
        "magnitude": round(s_mag, 2),
        "buzz": round(s_buzz, 2),
        "actionability": round(s_act, 2),
        "freshness": round(s_fresh, 2),
        "interest": round(s_int, 2),
        "raw": round(raw, 1),
        "category_weight": cat_w,
        "total": total,
    }
    return {
        "importance_total": total,
        "score_breakdown": json.dumps(breakdown, ensure_ascii=False),
        "rubric_version": cfg.rubric_version,
        "status": "scored",
    }


def _rank_score(story: dict[str, Any]) -> float:
    """排序主键：backlog 补位用的是衰减后的分数，fresh 用的是原分。"""
    decayed = story.get("_decayed")
    return float(decayed if decayed is not None else (story.get("importance_total") or 0))


def _title_key(story: dict[str, Any]) -> str:
    """标题归一化键：去掉标点、空格、大小写差异，用于识别同一事件。

    只用**原文标题**，不看译名：译名由 LLM 生成，换个译法键就变了，
    同一事件会认不出来（设计 §17.4 的同一原则：ID 不得依赖译名）。
    """
    raw = story.get("canonical_title") or story.get("title_zh") or ""
    return re.sub(r"[\s\W_]+", "", str(raw).lower())


def dedupe_same_title(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一事件被聚成两条 story 时只保留分数更高的一条。

    实测会出现「周日预览：全新 Battleforce 套装」同时存在 71.4 分（6h）
    与 67.0 分（117h）两条记录——两条 id 不同，前一天推过其中一条后，
    另一条仍不在 pushed_ids 里，就会造成重复推送。

    注意：这只是选槽层的兜底。真正要去重必须在聚类之后、写库之前合并
    （见 merge_same_title），否则被推过的那条先被 pushed_ids 过滤掉，
    这里根本看不到它（核查报告 N-02）。
    """
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for s in stories:
        key = _title_key(s)
        if not key:
            # 归一化后为空（纯符号标题）时不能丢，否则这条内容会静默消失
            key = f"__raw__:{s.get('id')}"
        prev = best.get(key)
        if prev is None:
            best[key] = s
            order.append(key)
            continue
        if _rank_score(s) > _rank_score(prev):
            best[key] = s
    return [best[k] for k in order if k in best]


def merge_same_title(
    stories: list[dict[str, Any]], pushed_ids: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """聚类之后、写库之前，把同标题的多条 story 合成一条（N-02）。

    返回 (保留的 stories, 被合并掉的 id 列表)。

    为什么要在这里做：`select_slots` 第一步就用 `pushed_ids` 过滤，
    被推过的那一条当场消失，去重逻辑再也看不到它，于是同标题的另一条
    第二天照常入选——正是要防的重复推送。在聚类层合并之后，被并掉的那条
    文档会重新挂到保留的事件上，它就变成"没有挂靠文档"，由 A-03 立即回收。

    合并规则（P1-4）：**已经推送过的 id 优先保留**——否则合并会把去重键换掉，
    明天新 id 不在台账里，同一事件会再被推一次；其次保留 first_seen 更早的。
    成员文档并集、claims 去重后合并，分数取较高者。
    """
    pushed = pushed_ids or set()
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for s in stories:
        key = _title_key(s) or f"__raw__:{s.get('id')}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)

    kept: list[dict[str, Any]] = []
    merged_ids: list[str] = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            kept.append(group[0])
            continue
        # 已推送 → 最早出现 → 分数高 → id 字典序（最后一级保证完全确定）
        group.sort(key=lambda s: (
            0 if str(s.get("id")) in pushed else 1,
            str(s.get("first_seen_at") or ""),
            -(s.get("importance_total") or 0),
            str(s.get("id") or ""),
        ))
        head = dict(group[0])
        members = {int(i) for i in (head.get("member_ids") or [])}
        claims = list(head.get("claims") or [])
        seen_claims = {normalize_claim(c.get("text") or "") for c in claims}
        best_score = head.get("importance_total") or 0
        for other in group[1:]:
            merged_ids.append(str(other.get("id")))
            members |= {int(i) for i in (other.get("member_ids") or [])}
            for c in other.get("claims") or []:
                norm = normalize_claim(c.get("text") or "")
                if norm in seen_claims:
                    continue
                seen_claims.add(norm)
                claims.append(c)
            best_score = max(best_score, other.get("importance_total") or 0)
            # 可信度取更保守（更强）的那一侧
            if other.get("authenticity") and (
                not head.get("authenticity")
                or str(other["authenticity"]) < str(head["authenticity"])
            ):
                head["authenticity"] = other["authenticity"]
        head["member_ids"] = sorted(members)
        head["claims"] = claims
        head["importance_total"] = best_score
        kept.append(head)
    return kept, merged_ids

def tie_break_key(story: dict[str, Any]) -> tuple:
    """§7.2 破平规则：总分相同 → 事件量级高 → 来源权威高 → 首发时间早。

    Python 的 sort 是稳定排序，但"同分随机"会让日报每天的顺序不可复现，
    调参时也无法对比。这里给出确定性的四段键（升序 = 从优到劣）。
    """
    return (
        -_rank_score(story),
        -float(story.get("magnitude_raw") or 0),
        -TIER_SCORE.get(str(story.get("authenticity") or "L2"), 0.0),
        str(story.get("published_at") or story.get("first_seen_at") or ""),
    )


# ---------------------------------------------------------------- 中文社区卡（§7.3）
# 设计 §7.3：中文社区影响面 30 / 可获得性 25 / 来源可信 20 / 稀缺性 15 / 时效 10
CN_WEIGHTS = {
    "affinity": 30.0,
    "obtainability": 25.0,
    "source_trust": 20.0,
    "scarcity": 15.0,
    "freshness": 10.0,
}

# 影响面：这条信息对中文玩家的实际影响
CN_AFFINITY_HINTS = (
    "中文", "简体", "繁体", "引进", "汉化", "国内", "中国区", "大陆", "国服",
    "本地", "代理", "出版", "翻译", "b站", "贴吧", "微博", "知乎", "微信",
)

# 可获得性：读者看完能不能真的买到 / 参加 / 看到
CN_OBTAIN_HINTS = (
    "预售", "预购", "开订", "发售", "上市", "库存", "补货", "断货", "限购",
    "淘宝", "闲鱼", "京东", "天猫", "门店", "实体店", "展会", "赛事", "报名",
    "直播", "b站", "视频", "下载", "试玩", "免费",
)

ZH_SOURCE_PREFIXES = ("bilibili_", "gnews_cn_", "gcores", "yystv")

# 常青内容：涂装教程、评测、战报、梗图。中文社区属性再强也不是「情报」。
CN_EVERGREEN_PATTERNS = (
    "涂装", "教程", "教学", "评测", "战报", "梗图", "开箱", "攻略", "二创", "手办",
    "从0开始", "新手入门", "开盒",
    # 娱乐向内容：meme / 整活 / 日常 vlog 不是「情报」，哪怕中文社区属性很强
    "meme", "梗", "表情包", "整活", "搞笑", "段子", "日常", "vlog", "直播回放", "切片",
    "动漫角色", "同人", "鬼畜", "沙雕",
    "review", "tutorial", "how to", "guide", "battle report", "painting",
)

CN_SOURCE_TRUST = {
    # 官方中文公告 > 机核/游研社 > B 站 UP > 其它中文聚合
    "gcores": 4.0,
    "yystv": 4.0,
    "bilibili": 3.0,
    "gnews_cn": 2.5,
}


def _hint_score(text: str, hints: tuple[str, ...], base: float, step: float) -> float:
    hits = sum(1 for h in hints if h in text)
    return max(0.0, min(5.0, base + step * hits))


def cn_source_trust(source_ids: list[str], category: str) -> float:
    if category == "official":
        return 5.0
    best = 2.0
    for sid in source_ids or []:
        for prefix, score in CN_SOURCE_TRUST.items():
            if str(sid).startswith(prefix):
                best = max(best, score)
    return best


def cn_scarcity(source_ids: list[str], n_sources: int) -> float:
    """稀缺性：英文源是否完全没有该信息。

    无法跨语言比对，用「来源是否全为中文源 + 来源数量」做代理：
    全部来自中文源且只有 1 家 → 这条信息很可能只有中文圈有。
    """
    ids = [str(s) for s in (source_ids or [])]
    if not ids:
        return 2.0
    zh_only = all(any(s.startswith(p) for p in ZH_SOURCE_PREFIXES) for s in ids)
    if zh_only:
        return 5.0 if n_sources <= 1 else 4.0
    return 3.0 if len(ids) <= 2 else 2.0


def score_cn_story(story: dict[str, Any], cfg: Config,
                  now: datetime | None = None) -> dict[str, Any]:
    """按 §7.3 的独立卡给中文社区事件打分（0–100），不复用通识卡。"""
    text = " ".join(
        str(story.get(k) or "")
        for k in ("title_zh", "canonical_title", "summary", "why_matters")
    ).lower()
    source_ids = story.get("source_ids") or []
    n_sources = int(story.get("n_sources") or 1)

    s_aff = _hint_score(text, CN_AFFINITY_HINTS, 3.5, 0.5)
    s_obt = _hint_score(text, CN_OBTAIN_HINTS, 2.0, 0.7)
    s_trust = cn_source_trust(source_ids, str(story.get("category") or ""))
    s_scar = cn_scarcity(source_ids, n_sources)
    s_fresh = freshness_score(story.get("published_at") or story.get("first_seen_at") or "", now)

    weights = cfg.get("scoring.cn_weights", {}) or {}
    total = 0.0
    for key, value in (
        ("affinity", s_aff), ("obtainability", s_obt), ("source_trust", s_trust),
        ("scarcity", s_scar), ("freshness", s_fresh),
    ):
        w = float(weights.get(key, CN_WEIGHTS[key]))
        total += w * value / 5

    # 常青内容惩罚（同 rules.EVERGREEN_PATTERNS 的口径）：
    # 涂装教程、评测、战报不是「情报」，即使中文社区属性很强也不该占槽 6
    penalty = 1.0
    if any(p in text for p in CN_EVERGREEN_PATTERNS):
        penalty = float(cfg.get("scoring.cn_evergreen_penalty", 0.65))
        total *= penalty

    breakdown = {
        "affinity": round(s_aff, 2),
        "obtainability": round(s_obt, 2),
        "source_trust": round(s_trust, 2),
        "scarcity": round(s_scar, 2),
        "freshness": round(s_fresh, 2),
        "evergreen_penalty": penalty,
        "total": round(total, 1),
    }
    return {"cn_score": round(total, 1), "cn_breakdown": json.dumps(breakdown, ensure_ascii=False)}


def diversify(
    items: list[dict[str, Any]],
    k: int = 4,
    jaccard_min: float = 0.18,
    containment_min: float = 0.28,
    min_shared: int = 2,
) -> list[dict[str, Any]]:
    """多样性约束：避免同一事件的多篇报道占满槽位。

    聚类是按标题相似度合并的，但「周日预告 / 周六开订」这类同事件不同角度
    的文章标题差异较大，会在选槽阶段再次互相挤占。这里做一次贪心去重：
    与已选项标题重叠度超过阈值的跳过，槽位不足时再回填。
    """
    picked: list[dict[str, Any]] = []
    picked_ids: set[str] = set()
    tokens: list[set[str]] = []

    for item in items:
        if len(picked) >= k:
            break
        title = item.get("canonical_title") or item.get("title_zh") or ""
        tok = token_set(title)
        duplicate = False
        for prev in tokens:
            shared = tok & prev
            if not shared:
                continue
            # 标题普遍较短，Jaccard 会低估重叠；同时看「共享词 / 较短标题」的包含度。
            # 但标题太短（<5 个有效词）时信号不足，不再用包含度判定，
            # 否则「Test story a」和「Test story b」会被当成同一件事。
            containment = len(shared) / max(1, min(len(tok), len(prev)))
            if len(shared) >= min_shared and (
                jaccard(tok, prev) >= jaccard_min
                or (min(len(tok), len(prev)) >= 5 and containment >= containment_min)
            ):
                duplicate = True
                break
        if duplicate:
            continue
        picked.append(item)
        picked_ids.add(item["id"])
        tokens.append(tok)

    # 不为了凑满 k 条把刚判为近重复的条目塞回来：设计 §8.4 明确「如实少推」，
    # 宁可这条日报只有 3 条，也不让同一件事占两个槽位（核查报告 BUG-27）
    return picked


# ---------------------------------------------------------------- 槽位
def select_slots(
    stories: list[dict[str, Any]],
    cfg: Config,
    pushed_ids: set[str],
    backlog: list[dict[str, Any]],
    threshold_scale: float = 1.0,
) -> dict[str, Any]:
    """按 §8.4 降级阶梯分配槽位。

    threshold_scale < 1 用于规则模式（无 LLM 时量级/可行动性维度天然偏低，
    沿用 LLM 模式的阈值会导致永远选不出内容）。
    """
    thresholds = cfg.get("thresholds", {}) or {}
    qualified_th = float(thresholds.get("qualified_score", 60)) * threshold_scale
    # 槽 6 用 §7.3 的独立卡打分，阈值口径与通识卡不同（设计给定 ≥60）
    cn_min = float(thresholds.get("cn_slot_min", 60)) * threshold_scale
    rumor_below = float(thresholds.get("rumor_promote_below", 55)) * threshold_scale
    backlog_min = float(thresholds.get("backlog_min_score", 45)) * threshold_scale
    push_hours = cfg.push_hours
    collect_hours = cfg.collect_hours
    aged_min = float(thresholds.get("aged_min_score", 50)) * threshold_scale
    # 二级补位的展示分下限：原始分够、但衰减之后已经没有推送价值的事件要被挡住（N-05）
    aged_min_shown = float(thresholds.get("aged_min_shown", 40)) * threshold_scale
    # 槽位数量走配置，不再硬编码 4/1/1（config.slots.* 以前是死配置，BUG-47）
    general_slots = max(1, int(cfg.get("slots.general", 4)))
    product_slots = max(0, int(cfg.get("slots.product", 1)))
    cn_slots = max(0, int(cfg.get("slots.cn_community", 1)))

    now = datetime.now(CST)
    # 一级窗口：48h 内，优先占槽
    cutoff = now - timedelta(hours=push_hours)
    # 二级窗口：一周内（采集窗口），权重不足时用来补位
    week_cutoff = now - timedelta(hours=collect_hours)
    # 中文社区源更新慢，槽 6 单独给一个更宽的时间窗（默认 3 天）
    cn_cutoff = now - timedelta(days=max(1, cfg.cn_slot_days))

    fresh: list[dict[str, Any]] = []      # 48h 内
    aged: list[dict[str, Any]] = []       # 48h ~ 一周
    cn_pool: list[dict[str, Any]] = []
    undated = 0
    for s in stories:
        if s["id"] in pushed_ids:
            continue
        dt = parse_dt(s.get("first_seen_at"))
        if not dt or dt < week_cutoff:
            continue
        # 时间来源不可信的 story 不进「今日新闻」候选：published_at 未知时无法判断新旧，
        # 宁可让它在报告页里可见，也不能当新文推给读者（C-01 / P1-1）。
        #
        # 曾经为了让一级池不空而「放行 L0」，结果是把 10 月的旧文当成今天的头条
        # （实测 13/21/20 天前）。现在日期可以从文章页 JSON-LD 回填（P0-1），
        # 所以这里恢复严格：拿不到日期就不进一级。
        if s.get("time_source") == "fetch" and not s.get("published_at"):
            undated += 1
            continue
        pub = parse_dt(s.get("published_at"))
        is_cn = s.get("impact_scope") == "cn_community"
        if pub and pub < week_cutoff and dt < week_cutoff:
            # 文章很旧 + 我们早就发现了 → 真过期，只有中文社区槽还考虑
            if is_cn and pub >= cn_cutoff:
                cn_pool.append(s)
            continue
        if pub and pub < week_cutoff:
            # 刚发现、但文章本身超过一周：**可以补位，不能当今日头条**。
            # 以前这里直接丢弃，导致延迟发现的高分老文永远推不出去（A-02）；
            # 现在放进 aged 池，按天数衰减后再参与补位。
            aged.append(s)
            if is_cn and pub >= cn_cutoff:
                cn_pool.append(s)
            continue
        # 分层按「首次出现」（设计 §20：一级 = 首次发现 ≤48h），
        # 文章日期只用于时效计分与「超过一周」的排除（P1-2）
        if dt >= cutoff:
            fresh.append(s)
            if is_cn and (not pub or pub >= cn_cutoff):
                cn_pool.append(s)
        else:
            # 48h 之外、一周之内：降级候选
            aged.append(s)
            if is_cn and pub and pub >= cn_cutoff:
                cn_pool.append(s)

    # 同一事件被聚成两条 story 时只留一条，避免跨天重复推送
    fresh = dedupe_same_title(fresh)
    aged = dedupe_same_title(aged)
    # 一级与二级之间也可能撞标题，二级里已有一级的那条要去掉
    fresh_keys = {_title_key(s) for s in fresh}
    aged = [s for s in aged if _title_key(s) not in fresh_keys]

    # §7.2 破平规则：同分时按 量级 → 权威 → 首发早 决定先后
    fresh.sort(key=tie_break_key)

    def pick_qualified(items):
        return [
            s for s in items
            if s.get("authenticity") != "L3" and (s.get("importance_total") or 0) >= qualified_th
        ]

    # 二级阈值：主阈值常常只剩个位数候选（实测 48h 池 518 条里 ≥55 分仅 4 条），
    # 一旦这 4 条里有已推送的，就会直接空日报。所以主阈值之后先退到二级阈值，
    # 再走商品/中文/传闻/历史补位——「宁可少推」仍然成立，但不要「明明有货却空着」。
    secondary_th = float(cfg.get("thresholds.secondary", 0) or 0)
    if secondary_th <= 0:
        secondary_th = max(0.0, qualified_th - 5)

    def pick_secondary(items):
        return [
            s for s in items
            if s.get("authenticity") != "L3"
            and (s.get("importance_total") or 0) >= secondary_th
        ]

    global_items = [s for s in fresh if s.get("impact_scope") != "cn_community"]
    # 补位优先级：通识 → 商品 → 中文社区 → 传闻 → 历史 backlog → 如实少推
    pool_primary = pick_qualified([s for s in global_items if s.get("event_type") != "mini"])
    pool_mini = pick_qualified([s for s in global_items if s.get("event_type") == "mini"])
    pool_cn = pick_qualified([s for s in fresh if s.get("impact_scope") == "cn_community"])
    pool_rumor = sorted(
        [
            s for s in fresh
            if s.get("authenticity") == "L3" and (s.get("importance_total") or 0) >= rumor_below
        ],
        key=lambda s: s.get("importance_total") or 0,
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    mode = "normal"

    def take(items: list[dict[str, Any]], reason: str) -> int:
        nonlocal mode
        chosen = {x["id"] for x in selected}
        added = 0
        for item in items:
            if len(selected) >= general_slots:
                break
            if item["id"] in chosen:
                continue
            selected.append({**item, "_slot_reason": reason})
            chosen.add(item["id"])
            added += 1
        if added:
            mode = {"rumor": "rumor_fill", "cn_fill": "cn_fill",
                "secondary": "secondary_fill"}.get(reason, mode)
        return added

    take(pool_primary, "qualified")
    if len(selected) < general_slots:
        # 主阈值不够就先降到二级阈值，而不是直接跳到商品/中文槽
        take([s for s in pick_secondary(
            [x for x in global_items if x.get("event_type") != "mini"])
            if s["id"] not in {x["id"] for x in pool_primary}], "secondary")
    if len(selected) < general_slots:
        take(pool_mini, "qualified")
    if len(selected) < general_slots:
        take(pool_cn, "cn_fill")
    if len(selected) < general_slots:
        take(pool_rumor, "rumor")

    # 二级补位：48h 之外、一周之内的合格事件（按天数轻微衰减）
    if len(selected) < general_slots and aged:
        aged_candidates = []
        for item in aged:
            if item.get("authenticity") == "L3":
                continue
            score = item.get("importance_total") or 0
            if score < aged_min:
                continue
            # 衰减按「事件首次出现」的天数，与分层口径保持一致（P1-2）
            ref = parse_dt(item.get("first_seen_at"))
            age_days = max(0.0, (now - ref).total_seconds() / 86400) if ref else 0.0
            decayed = round(score * (DECAY_PER_DAY ** age_days), 1)
            # 原始分过线还不够：衰减后太低就等于把旧闻硬塞给读者
            if decayed < aged_min_shown:
                continue
            aged_candidates.append(
                (decayed, {**item, "_slot_reason": "aged", "_decayed": decayed})
            )
        aged_candidates.sort(key=lambda x: x[0], reverse=True)
        before = len(selected)
        for _, item in aged_candidates:
            if len(selected) >= general_slots:
                break
            selected.append(item)
        if len(selected) > before:
            mode = "aged_fill" if before else "aged_only"

    if len(selected) < general_slots:
        selected_ids = {x["id"] for x in selected}
        candidates = []
        for item in backlog:
            # 与 fresh 一样必须自己再挡一次已推送：不能依赖调用方预先过滤，
            # 一旦上游漏了就会造成重复推送。
            if item["id"] in selected_ids or item["id"] in pushed_ids:
                continue
            if (item.get("importance_total") or 0) < backlog_min:
                continue
            dt = parse_dt(item.get("first_seen_at"))
            age_days = max(0.0, (now - dt).total_seconds() / 86400) if dt else 0.0
            decayed = (item.get("importance_total") or 0) * (DECAY_PER_DAY ** age_days)
            candidates.append((decayed, {**item, "_slot_reason": "backlog", "_decayed": round(decayed, 1)}))
        candidates.sort(key=lambda x: x[0], reverse=True)
        before = len(selected)
        for _, item in candidates:
            if len(selected) >= general_slots:
                break
            selected.append(item)
        if len(selected) > before:
            mode = "backlog_fill" if before else "backlog_only"

    if len(selected) < general_slots and mode == "normal":
        mode = "sparse" if selected else "empty"

    # 槽 5：模型商品表（P0 无价格数据，用 mini 类事件构成）
    # 必须排除已进通用槽的条目，否则同一条会在 ③④ 和 ⑤ 重复出现
    selected_ids = {x["id"] for x in selected}
    product_items = [
        s for s in fresh
        if s["id"] not in pushed_ids
        and s["id"] not in selected_ids
        and is_product_item(s)
    ]
    product_items.sort(key=tie_break_key)
    product = product_items[:product_slots]

    # 槽 6：中文社区（§7.3 独立卡 + 更宽的时间窗）
    cn_mag_min = float(thresholds.get("cn_min_magnitude", 2))
    cn_candidates = []
    seen_cn: set[str] = set()
    for s in cn_pool:
        if s["id"] in selected_ids or s["id"] in seen_cn:
            continue
        seen_cn.add(s["id"])
        # 质量底线：meme/日常向内容的量级通常是 1，直接不进槽 6
        if float(s.get("magnitude_raw") or 0) < cn_mag_min:
            continue
        pub = parse_dt(s.get("published_at") or s.get("first_seen_at"))
        age_days = max(0.0, (now - pub).total_seconds() / 86400) if pub else 0.0
        scored = {**s, **score_cn_story(s, cfg), "age_days": round(age_days, 1)}
        if scored["cn_score"] >= cn_min:
            cn_candidates.append(scored)
    # 同分仍按cn卡的五个分项破平，保证顺序可复现
    cn_candidates.sort(
        key=lambda s: (-s["cn_score"], -float(json.loads(s["cn_breakdown"])["source_trust"]))
    )
    cn_slot = cn_candidates[0] if (cn_slots and cn_candidates) else None

    ranked = sorted(selected, key=tie_break_key)
    # 已选满时再取几名候选做多样性替换的备选，
    # 避免「去重后只剩 3 条」而白白浪费槽位
    if len(ranked) >= general_slots:
        chosen = {x["id"] for x in ranked}
        extras = [
            s for s in (pool_primary + pool_mini + pool_cn + pool_rumor)
            if s["id"] not in chosen
        ]
        extras.sort(key=tie_break_key)
        ranked = ranked + [{**s, "_slot_reason": "qualified"} for s in extras[:general_slots]]

    picked_general = diversify(ranked, k=general_slots)
    # 选完后再按破平键统一重排，保证「第 N 条的分数不低于第 N+1 条」这个契约不被破坏
    picked_general.sort(key=tie_break_key)

    # 槽 5 为空但商品类新闻已在通识槽里时，给出交叉引用而不是重复列出
    product_refs: list[int] = []
    if not product:
        product_refs = [
            idx for idx, item in enumerate(picked_general, 1)
            if item.get("event_type") == "mini"
        ]

    return {
        "mode": mode,
        "general": picked_general,
        "product": product,
        "product_refs": product_refs,
        "cn": cn_slot,
        "stats": {
            "fresh": len(fresh),
            "aged_pool": len(aged),
            "qualified": len(pool_primary) + len(pool_mini) + len(pool_cn),
            "rumor_pool": len(pool_rumor),
            "cn_pool": len([s for s in fresh if s.get("impact_scope") == "cn_community"]),
            "backlog_pool": len(backlog),
            "undated_skipped": undated,
        },
    }


def run_analysis(docs: list[dict[str, Any]], client: Any, enabled: bool) -> dict[int, dict[str, Any]]:
    """对文档做 LLM 分析；失败或未启用时用规则结果兜底。"""
    results: dict[int, dict[str, Any]] = {}
    if enabled and client.enabled:
        results = client.analyze_batch(docs)
    for idx, doc in enumerate(docs):
        if idx not in results:
            results[idx] = fallback_analysis(doc)
    return results


def hours_ago(hours: int) -> datetime:
    return datetime.now(CST) - timedelta(hours=hours)


