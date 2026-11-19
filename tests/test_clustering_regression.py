"""聚类回归测试。

锁定第十四轮修掉的严重 bug：泛化实体（warhammer 40k / games workshop / space marines …）
出现在几乎所有文章里，导致「共享 ≥2 实体即合并」把 25 篇无关文章并成一个簇，
当天最高价值的新闻（战争黎明 IV 预告，79.4 分）因此静默消失。

这些用例使用事故当天的真实实体组合，防止同类回归。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.pipeline import (  # noqa: E402
    GENERIC_ENTITIES,
    MIN_SHARED_ENTITIES,
    cluster_docs,
    score_story,
)
from test_pipeline import make_config  # noqa: E402

PUB = "2026-11-17T14:00:00+08:00"


def doc(
    title: str,
    source_id: str,
    entities: list[str],
    tier: str = "L0",
    domain: str | None = None,
) -> dict:
    """构造一条 raw_doc。

    domain 默认与 source_id 绑定——现实中不同采集源对应不同站点。
    要模拟「同一篇内容被多个查询源重复收录」，显式传入同一个 domain。
    """
    host = domain or f"{source_id}.example"
    return {
        "source_id": source_id,
        "title": title,
        "url": f"https://{host}/{abs(hash(title)) % 10**8}",
        "published_at": PUB,
        "fetched_at": PUB,
        "discovered_at": PUB,
        "tier": tier,
        "lang": "en",
        "body": "",
        "analysis": None,
    }


def ana(entities: list[str], title_zh: str = "译名") -> dict:
    return {
        "title_zh": title_zh,
        "event_type": "other",
        "category": "40k",
        "impact_scope": "global",
        "magnitude": 3.0,
        "actionability": 2.0,
        "entities": entities,
        "claims": [],
        "is_rumor": False,
    }


# 事故当天的真实实体
DOW = ["Dawn of War IV", "Blood Ravens", "Dark Angels", "Orks", "Necrons", "Adeptus Mechanicus"]
INTERCESSORS = [
    "auspex tactics", "eradicators", "games workshop", "gravis armour",
    "heavy intercessors", "space marines",
]
KHEMRI_A = ["khemri", "tomb kings", "warhammer community", "warhammer: the old world"]
KHEMRI_B = ["khemri", "tomb kings", "warhammer community", "warhammer: the old world"]


class TestGenericEntityGuard(unittest.TestCase):
    def test_stoplist_covers_franchise_wide_terms(self):
        for term in ("warhammer 40k", "games workshop", "space marines",
                     "warhammer community", "age of sigmar", "reddit", "b站"):
            self.assertIn(term, GENERIC_ENTITIES, f"{term} 必须在泛化实体表里")

    def test_unrelated_articles_sharing_generic_entities_do_not_merge(self):
        """战争黎明 IV 与 Heavy Intercessors 只共享泛化实体，绝不能合并。"""
        docs = [
            doc("Huge Warhammer 40,000: Dawn of War IV game overview trailer revealed", "whc_news", DOW),
            doc("Heavy Intercessors - Has GW Finally Fixed Them?", "yt_auspex", INTERCESSORS),
        ]
        stories = cluster_docs(docs, {0: ana(DOW), 1: ana(INTERCESSORS)})
        self.assertEqual(len(stories), 2, "两篇无关文章被错误合并")
        titles = " ".join(s["canonical_title"] for s in stories)
        self.assertIn("Dawn of War IV", titles)
        self.assertIn("Heavy Intercessors", titles)

    def test_many_articles_with_only_generic_overlap_stay_separate(self):
        """构造 10 篇各自话题不同、但都含泛化实体的文章，不得滚成一个大簇。"""
        # 标题必须彼此不相似，否则测的是标题聚类而不是实体过滤
        headlines = {
            "Necromunda": "Hive war expansion brings new gang rules to the underhive",
            "Blood Bowl": "Season three kicks off with three brand new star players",
            "Kill Team": "Approved ops pack adds jungle terrain and stealth missions",
            "Black Library": "Siege novel trilogy concludes with a limited hardback",
            "Forge World": "Resin dreadnought variant joins the solar auxilia range",
            "Warhammer Plus": "Animation bundle adds two shorts and a painting masterclass",
            "Golden Demon": "Golden Demon entries open for the spring competition",
            "Necrons": "Dynasty rebalance shifts points across three datasheets",
            "Aeldari": "Craftworld aspect warriors get a plastic refresh",
            "Tyranids": "Hive fleet detachment rules arrive in the next campaign book",
        }
        docs, analyses = [], {}
        for i, (topic, headline) in enumerate(headlines.items()):
            ents = ["warhammer 40k", "games workshop", "space marines", topic.lower()]
            docs.append(doc(headline, f"src{i}", ents, "L1"))
            analyses[i] = ana(ents)
        stories = cluster_docs(docs, analyses)
        self.assertEqual(len(stories), 10, f"泛化实体导致合并，实际只剩 {len(stories)} 个事件")

    def test_specific_entity_overlap_still_merges(self):
        """共享两个「具体」实体的同一事件仍应合并（不能矫枉过正）。"""
        docs = [
            doc("Sunday Preview – The legions of Khemri arise!", "whc_news", KHEMRI_A),
            doc("Warhammer Next Week: The New Legions Of Khemri", "gnews_40k", KHEMRI_B, "L2"),
        ]
        stories = cluster_docs(docs, {0: ana(KHEMRI_A), 1: ana(KHEMRI_B)})
        self.assertEqual(len(stories), 1, "同一事件的多个来源未能合并")
        self.assertEqual(stories[0]["n_sources"], 2)

    def test_single_shared_specific_entity_is_not_enough(self):
        """只共享 1 个具体实体时不应合并——阈值是 2。"""
        a = ["khemri", "tomb kings"]
        b = ["khemri", "bretonnia"]
        docs = [
            doc("Legions of Khemri arise in the old world", "s1", a),
            doc("Bretonnia returns alongside Khemri", "s2", b, "L1"),
        ]
        stories = cluster_docs(docs, {0: ana(a), 1: ana(b)})
        self.assertEqual(len(stories), 2)
        self.assertGreaterEqual(MIN_SHARED_ENTITIES, 2)


class TestIndependentSourceCount(unittest.TestCase):
    """BUG-03：多源印证的「源」必须是不同发布域名，而不是采集配置。"""

    ENT = ["khemri", "tomb kings"]

    def test_same_domain_does_not_count_as_two_sources(self):
        docs = [
            doc("Legions of Khemri arise", "bilibili_wh", self.ENT, "L2", domain="bilibili.com"),
            doc("Legions of Khemri arise", "bilibili_40k", self.ENT, "L2", domain="bilibili.com"),
        ]
        stories = cluster_docs(docs, {0: ana(self.ENT), 1: ana(self.ENT)})
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["n_sources"], 1, "同一域名的重复收录不能算两个来源")

    def test_same_domain_l2_is_not_upgraded(self):
        docs = [
            doc("Legions of Khemri arise", "bilibili_wh", self.ENT, "L2", domain="bilibili.com"),
            doc("Legions of Khemri arise", "bilibili_model", self.ENT, "L2", domain="bilibili.com"),
            doc("Legions of Khemri arise", "bilibili_40k", self.ENT, "L2", domain="bilibili.com"),
        ]
        stories = cluster_docs(docs, {i: ana(self.ENT) for i in range(3)})
        self.assertEqual(stories[0]["authenticity"], "L2", "同源重复收录不得升级为 L1")
        self.assertNotIn("多源印证", stories[0]["authenticity_note"])

    def test_distinct_domains_do_upgrade(self):
        docs = [
            doc("Legions of Khemri arise", "gnews_40k", self.ENT, "L2", domain="spikeybits.com"),
            doc("Legions of Khemri arise", "wargamer", self.ENT, "L2", domain="wargamer.com"),
        ]
        stories = cluster_docs(docs, {0: ana(self.ENT), 1: ana(self.ENT)})
        self.assertEqual(stories[0]["n_sources"], 2)
        self.assertEqual(stories[0]["authenticity"], "L1")

    def test_buzz_not_inflated_by_duplicate_intake(self):
        one = [
            doc("Legions of Khemri arise", "bilibili_wh", self.ENT, "L2", domain="bilibili.com"),
            doc("Legions of Khemri arise", "bilibili_40k", self.ENT, "L2", domain="bilibili.com"),
            doc("Legions of Khemri arise", "bilibili_model", self.ENT, "L2", domain="bilibili.com"),
        ]
        stories = cluster_docs(one, {i: ana(self.ENT) for i in range(3)})
        self.assertEqual(stories[0]["n_sources"], 1)
        breakdown = json.loads(
            score_story(stories[0], make_config(), {})["score_breakdown"]
        )
        self.assertEqual(breakdown["buzz"], 1.0, "重复收录不应抬高传播热度")


class TestBlobSizeGuard(unittest.TestCase):
    def test_cluster_size_never_explodes(self):
        """真实语料里最大的簇只有几条来源；出现超大簇说明又过拟了。"""
        docs, analyses = [], {}
        for i in range(12):
            ents = ["dawn of war iv", "blood ravens"] if i < 3 else (
                ["warhammer 40k", "games workshop", "space marines", f"topic{i}"]
            )
            headline = ("Dawn of War four trailer breakdown and faction list"
                        if i < 3 else
                        ["Cadia stands campaign book arrives", "Necron tomb worlds article",
                         "Ork buggy kit review roundup", "Aeldari corsair fleet reveal",
                         "Space wolf upgrade sprue first look", "Tau auxiliaries codex leak",
                         "Sisters of battle squad refresh", "Tyranid hive tyrant build",
                         "Kill team terrain expansion", "Old world bretonnia return",
                         "Horus heresy tank variants", "Black library audio drama"][i - 3])
            docs.append(doc(headline, f"s{i}", ents, "L1"))
            analyses[i] = ana(ents)
        stories = cluster_docs(docs, analyses)
        biggest = max(len(s["source_ids"]) for s in stories)
        self.assertLessEqual(biggest, 4, f"出现超大簇（{biggest} 个来源），疑似泛化实体未过滤")


if __name__ == "__main__":
    unittest.main(verbosity=2)
