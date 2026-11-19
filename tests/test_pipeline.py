"""核心逻辑单元测试（不依赖网络与 LLM）。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.adapters import content_hash, clean_text  # noqa: E402
from infotwitter.config import Config  # noqa: E402
from infotwitter.db import CST  # noqa: E402
from infotwitter.pipeline import (  # noqa: E402
    buzz_score,
    cluster_docs,
    freshness_score,
    jaccard,
    score_story,
    select_slots,
    token_set,
)
from infotwitter.pricing import (  # noqa: E402
    classify_category,
    classify_line,
    clean_query,
    query_variants,
    render_summary,
    to_cny,
)
from infotwitter.render import build_push_text  # noqa: E402


def make_config(**overrides) -> Config:
    data = {
        "windows": {"collect_hours": 48, "push_hours": 24, "backlog_days": 7},
        "scoring": {
            "rubric_version": "importance@v1",
            "weights": {
                "authority": 20, "magnitude": 25, "buzz": 20,
                "actionability": 15, "freshness": 10, "interest": 10,
            },
            "category_weights": {"40k": 1.0, "30k": 1.0, "aos": 0.85, "tow": 0.85, "other": 0.6},
        },
        "thresholds": {
            "qualified_score": 60, "cn_slot_min": 60,
            "backlog_min_score": 45, "rumor_promote_below": 55,
        },
    }
    for key, value in overrides.items():
        node = data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(data, [])


_HEADLINES = [
    "New Ork codex arriving in December with twelve datasheets",
    "Aeldari aspect warriors get a plastic refresh next quarter",
    "Necron dynasty rebalance shifts points across three datasheets",
    "Tyranid hive fleet detachment rules arrive in the campaign book",
    "Adeptus Custodes golden armour refresh revealed at the open day",
    "Kill Team jungle terrain set adds stealth missions to the box",
    "Black Library siege trilogy concludes with a limited hardback",
    "Forge World resin dreadnought variant joins solar auxilia range",
    "Golden Demon entries open for the spring painting competition",
    "Warhammer Plus animation bundle adds two shorts and a class",
]
_headline_counter = {"i": 0}


def make_story(**kw):
    # 默认标题按顺序取不同头条：不同 id 就是不同事件。
    # 若用同一个短标题，多样性约束会把它们当成近重复而丢弃（设计意图）。
    _id = kw.get("id", "s1")
    _title = kw.get("title")
    if not _title:
        _title = _HEADLINES[_headline_counter["i"] % len(_HEADLINES)]
        _headline_counter["i"] += 1
    base = {
        "id": _id,
        "canonical_title": _title,
        "title_zh": kw.get("title_zh", ""),
        "event_type": kw.get("event_type", "mini"),
        "category": kw.get("category", "40k"),
        "impact_scope": kw.get("impact_scope", "global"),
        "authenticity": kw.get("authenticity", "L0"),
        "first_seen_at": kw.get("first_seen_at", datetime.now(CST).isoformat()),
        "n_sources": kw.get("n_sources", 1),
        "magnitude_raw": kw.get("magnitude", 3.0),
        "actionability_raw": kw.get("actionability", 3.0),
        "entities": kw.get("entities", []),
        "importance_total": kw.get("score", 70.0),
        "primary_url": "https://example.com/a",
        "source_ids": ["s"],
    }
    base.update({k: v for k, v in kw.items() if k not in base})
    return base


class TestTextHelpers(unittest.TestCase):
    def test_clean_text_strips_tags(self):
        self.assertEqual(clean_text("<p>Hello <b>World</b></p>"), "Hello World")

    def test_content_hash_stable(self):
        self.assertEqual(content_hash("Title", "http://a"), content_hash("title ", "http://a"))
        self.assertNotEqual(content_hash("Title", "http://a"), content_hash("Title", "http://b"))

    def test_token_set_drops_stopwords(self):
        tokens = token_set("The New Warhammer 40k Battleforce Boxes")
        self.assertNotIn("the", tokens)
        self.assertNotIn("warhammer", tokens)
        self.assertIn("battleforce", tokens)

    def test_jaccard_bounds(self):
        self.assertEqual(jaccard({"a", "b"}, {"a", "b"}), 1.0)
        self.assertEqual(jaccard(set(), {"a"}), 0.0)


class TestPricing(unittest.TestCase):
    def test_to_cny(self):
        self.assertEqual(to_cny(10, "GBP", {"GBP": 9.2}), 92.0)
        self.assertIsNone(to_cny(10, "JPY", {"GBP": 9.2}))

    def test_classify_line_40k(self):
        self.assertEqual(classify_line("ADEPTUS CUSTODES: CUSTODIAN GUARD"), "40k")
        self.assertEqual(classify_line("CSM: LORD DISCORDANT ON HELSTALKER"), "40k")
        self.assertEqual(classify_line("A/MILITARUM: ROGAL DORN BATTLE TANK"), "40k")

    def test_classify_line_other_lines(self):
        self.assertEqual(classify_line("LEGIONS IMPERIALIS: THE HORUS HERESY"), "30k")
        self.assertEqual(classify_line("Warhammer Age of Sigmar: Skaven"), "aos")
        self.assertEqual(classify_line("The Old World: Tomb Kings"), "tow")

    def test_classify_category(self):
        self.assertEqual(classify_category("CODEX: ADEPTUS CUSTODES"), "codex")
        self.assertEqual(classify_category("COMBAT PATROL: BATTLEZONE"), "terrain")
        self.assertEqual(classify_category("B/Z STC HAB-BUNKER"), "terrain")
        self.assertEqual(classify_category("BASE PAINT SET"), "paint")
        self.assertEqual(classify_category("BLACK LIBRARY NOVEL"), "book")

    def test_clean_query_strips_language_marker(self):
        self.assertEqual(clean_query("WARHAMMER 40000: INTRODUCTORY SET (CHN)"),
                         "warhammer 40000 introductory set")

    def test_query_variants_shortens(self):
        variants = query_variants("WARHAMMER 40000: INTRODUCTORY SET (CHN)")
        self.assertGreaterEqual(len(variants), 2)
        self.assertIn("introductory set", variants)
        # 变体应当由长到短、互不重复
        self.assertEqual(len(variants), len(set(variants)))

    def test_render_summary_empty(self):
        self.assertIn("没有匹配", render_summary([]))


class TestClustering(unittest.TestCase):
    def test_merges_near_duplicate_titles(self):
        docs = [
            {"source_id": "a", "title": "New Battleforce boxes revealed for Warhammer 40,000",
             "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L0", "url": "http://a", "analysis": None},
            {"source_id": "b", "title": "New Battleforce boxes revealed for Warhammer 40k",
             "published_at": "2026-11-17T08:30:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L1", "url": "http://b", "analysis": None},
        ]
        stories = cluster_docs(docs, {})
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["n_sources"], 2)

    def test_keeps_distinct_stories_apart(self):
        docs = [
            {"source_id": "a", "title": "Golden Demon 2026 finalists announced",
             "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L0", "url": "http://a", "analysis": None},
            {"source_id": "b", "title": "Dawn of War IV gameplay trailer released",
             "published_at": "2026-11-17T08:10:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L1", "url": "http://b", "analysis": None},
        ]
        self.assertEqual(len(cluster_docs(docs, {})), 2)

    def test_l3_not_upgraded_by_other_l3(self):
        """L3 不因其他 L3 印证而升级，防止同源洗稿。"""
        docs = [
            {"source_id": "a", "title": "Skaven new battletome rules preview details",
             "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L3", "url": "http://a", "analysis": None},
            {"source_id": "b", "title": "Skaven new battletome rules preview details",
             "published_at": "2026-11-17T08:05:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L3", "url": "http://b", "analysis": None},
        ]
        stories = cluster_docs(docs, {})
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["authenticity"], "L3")

    def test_l2_upgrades_with_corroboration(self):
        docs = [
            {"source_id": "a", "title": "New Ork codex arriving in December",
             "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L2", "url": "http://a", "analysis": None},
            {"source_id": "b", "title": "New Ork codex arriving in December",
             "published_at": "2026-11-17T08:05:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L2", "url": "http://b", "analysis": None},
        ]
        stories = cluster_docs(docs, {})
        self.assertEqual(stories[0]["authenticity"], "L1")

    def test_rumor_wording_caps_at_l3(self):
        docs = [{"source_id": "a", "title": "GW leaks missing Imperial Knight variant",
                 "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
                 "tier": "L1", "url": "http://a", "analysis": None}]
        stories = cluster_docs(docs, {})
        self.assertEqual(stories[0]["authenticity"], "L3")

    def test_prefers_real_url_over_googlenews(self):
        docs = [
            {"source_id": "a", "title": "Sunday Preview legions arise",
             "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L0", "url": "https://news.google.com/rss/articles/XYZ", "analysis": None},
            {"source_id": "b", "title": "Sunday Preview legions arise",
             "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
             "tier": "L0", "url": "https://www.warhammer-community.com/en-gb/articles/abc/x/",
             "analysis": None},
        ]
        stories = cluster_docs(docs, {})
        self.assertTrue(stories[0]["primary_url"].startswith("https://www.warhammer-community.com"))

    def test_story_id_stable_across_translations(self):
        """译名变化不得改变 story_id，否则台账去重会失效导致重复推送。"""
        doc = {
            "source_id": "a", "title": "New Battleforce boxes revealed",
            "published_at": "2026-11-17T08:00:00+08:00", "fetched_at": "2026-11-17T09:00:00+08:00",
            "tier": "L0", "url": "http://a", "analysis": None,
        }
        a1 = {"title_zh": "全新战团包公布", "event_type": "mini", "fallback": False}
        a2 = {"title_zh": "新的战斗力量盒揭晓", "event_type": "mini", "fallback": False}
        id1 = cluster_docs([doc], {0: a1})[0]["id"]
        id2 = cluster_docs([doc], {0: a2})[0]["id"]
        self.assertEqual(id1, id2)


class TestLedger(unittest.TestCase):
    """台账去重必须只认「送达成功」，否则 webhook 抖一下最好的几条就永久丢失。"""

    def setUp(self):
        import tempfile

        from infotwitter.db import Database

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Database(Path(self.tmp.name))

    def tearDown(self):
        self.db.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _make_story(self, story_id: str) -> None:
        self.db.conn.execute(
            "INSERT OR IGNORE INTO stories (id, canonical_title, first_seen_at, last_seen_at, status) "
            "VALUES (?,?,?,?,'scored')",
            (story_id, story_id, "2026-11-17T08:00:00+08:00", "2026-11-17T08:00:00+08:00"),
        )
        self.db.conn.commit()

    def _record(self, slot: int, story_id: str, ok: bool):
        self._make_story(story_id)
        self.db.record_push({
            "push_date": "2026-11-17", "slot": slot, "story_id": story_id,
            "title": story_id, "importance": 70, "channel": "test",
            "payload": "x", "delivery_ok": 1 if ok else 0,
        })

    def test_failed_delivery_is_retried(self):
        self._record(1, "s1", ok=False)
        self.assertEqual(self.db.pushed_story_ids(), set())
        self.assertFalse(self.db.already_pushed_slot("2026-11-17", 1))

    def test_successful_delivery_is_excluded(self):
        self._record(1, "s1", ok=True)
        self.assertEqual(self.db.pushed_story_ids(), {"s1"})
        self.assertTrue(self.db.already_pushed_slot("2026-11-17", 1))

    def test_force_ignores_today(self):
        self._record(1, "s1", ok=True)
        self.assertEqual(self.db.pushed_story_ids(except_date="2026-11-17"), set())


class TestRulesEngine(unittest.TestCase):
    def test_business_event(self):
        from infotwitter.rules import classify

        r = classify("Games Workshop half-year financial results", "")
        self.assertEqual(r["event_type"], "business")
        self.assertGreaterEqual(r["magnitude"], 4)

    def test_category_30k(self):
        from infotwitter.rules import classify

        self.assertEqual(classify("New Horus Heresy core mission pack", "")["category"], "30k")

    def test_rumor_marker(self):
        from infotwitter.rules import classify

        self.assertTrue(classify("Rumour: new codex leak", "")["is_rumor"])

    def test_chinese_source_is_cn_scope(self):
        from infotwitter.rules import classify

        self.assertEqual(classify("战锤新模型发售", "", "zh")["impact_scope"], "cn_community")

    def test_english_default_is_global(self):
        from infotwitter.rules import classify

        self.assertEqual(classify("New Space Marine kit", "", "en")["impact_scope"], "global")

    def test_unknown_is_conservative(self):
        from infotwitter.rules import classify

        r = classify("Some random post title", "")
        self.assertEqual(r["event_type"], "other")
        self.assertEqual(r["category"], "other")


class TestScoring(unittest.TestCase):
    def test_freshness_decay(self):
        """曲线覆盖一周窗口：越老越低，但一周内仍有区分度。"""
        now = datetime.now(CST)
        self.assertEqual(freshness_score((now - timedelta(hours=1)).isoformat()), 5.0)
        self.assertEqual(freshness_score((now - timedelta(hours=30)).isoformat()), 3.0)
        self.assertEqual(freshness_score((now - timedelta(hours=60)).isoformat()), 1.5)
        self.assertEqual(freshness_score((now - timedelta(hours=100)).isoformat()), 1.0)
        self.assertEqual(freshness_score((now - timedelta(hours=150)).isoformat()), 0.6)
        self.assertEqual(freshness_score((now - timedelta(hours=200)).isoformat()), 0.2)

    def test_buzz_monotonic(self):
        self.assertEqual(buzz_score(1), 1.0)
        self.assertGreater(buzz_score(3), buzz_score(2))
        self.assertLessEqual(buzz_score(50), 5.0)

    def test_score_bounds(self):
        cfg = make_config()
        story = make_story()
        result = score_story(story, cfg, {})
        self.assertGreaterEqual(result["importance_total"], 0)
        self.assertLessEqual(result["importance_total"], 100)

    def test_category_weight_applied(self):
        cfg = make_config()
        core = score_story(make_story(category="40k"), cfg, {})["importance_total"]
        other = score_story(make_story(category="other"), cfg, {})["importance_total"]
        self.assertGreater(core, other)

    def test_low_authority_scores_lower(self):
        cfg = make_config()
        high = score_story(make_story(authenticity="L0"), cfg, {})["importance_total"]
        low = score_story(make_story(authenticity="L3"), cfg, {})["importance_total"]
        self.assertGreater(high, low)


class TestSlotSelection(unittest.TestCase):
    def test_takes_top_four_qualified(self):
        cfg = make_config()
        stories = [make_story(id=f"s{i}", score=70 + i) for i in range(6)]
        slots = select_slots(stories, cfg, set(), [])
        self.assertEqual(len(slots["general"]), 4)
        self.assertEqual(slots["general"][0]["id"], "s5")

    def test_rumor_never_enters_as_qualified(self):
        cfg = make_config()
        stories = [make_story(id="r1", authenticity="L3", score=95)]
        slots = select_slots(stories, cfg, set(), [])
        self.assertEqual(len(slots["general"]), 1)
        self.assertEqual(slots["general"][0]["_slot_reason"], "rumor")
        self.assertEqual(slots["mode"], "rumor_fill")

    def test_rumor_fills_when_short(self):
        cfg = make_config()
        stories = [
            make_story(id="a", score=70, event_type="rules"),
            make_story(id="r1", authenticity="L3", score=62),
        ]
        slots = select_slots(stories, cfg, set(), [])
        ids = [s["id"] for s in slots["general"]]
        self.assertIn("a", ids)
        self.assertIn("r1", ids)
        self.assertEqual(slots["mode"], "rumor_fill")

    def test_weak_rumor_not_promoted(self):
        cfg = make_config()
        stories = [
            make_story(id="a", score=70, event_type="rules"),
            make_story(id="r1", authenticity="L3", score=40),
        ]
        slots = select_slots(stories, cfg, set(), [])
        ids = [s["id"] for s in slots["general"]]
        self.assertEqual(ids, ["a"])

    def test_backlog_fill_with_decay(self):
        cfg = make_config()
        old = (datetime.now(CST) - timedelta(days=2)).isoformat()
        backlog = [make_story(id="b1", score=55, first_seen_at=old)]
        slots = select_slots([], cfg, set(), backlog)
        self.assertEqual(len(slots["general"]), 1)
        self.assertEqual(slots["general"][0]["_slot_reason"], "backlog")
        self.assertLess(slots["general"][0]["_decayed"], 55)

    def test_pushed_stories_excluded(self):
        cfg = make_config()
        stories = [make_story(id="a", score=70)]
        slots = select_slots(stories, cfg, {"a"}, [])
        self.assertEqual(len(slots["general"]), 0)

    def test_cn_slot_picks_chinese_scope(self):
        cfg = make_config()
        stories = [
            make_story(id=f"g{i}", score=70 + i, event_type="rules") for i in range(4)
        ] + [
            make_story(
                id="c1", score=65, impact_scope="cn_community",
                title="战锤40K 中文版新规则抢先看，B站首发",
                summary="国内玩家可在 B 站观看，预售同步开订",
            )
        ]
        slots = select_slots(stories, cfg, set(), [])
        self.assertEqual(len(slots["general"]), 4)
        self.assertIsNotNone(slots["cn"])
        self.assertEqual(slots["cn"]["id"], "c1")
        # §7.3：槽 6 有自己的分数，不是通识卡分数
        self.assertIn("cn_score", slots["cn"])
        self.assertGreaterEqual(slots["cn"]["cn_score"], 60)

    def test_cn_slot_uses_its_own_scorecard(self):
        """中文社区卡与通识卡是两套分数，不能互相顶替。"""
        from infotwitter.pipeline import score_cn_story

        cfg = make_config()
        story = make_story(
            id="c2", score=30, impact_scope="cn_community",
            title="战锤中文维基上线，国内玩家可查简体中文规则",
            summary="B站与贴吧同步更新，淘宝已有中文版预售",
            entity_type=None,
        )
        scored = score_cn_story(story, cfg)
        self.assertGreater(scored["cn_score"], story["importance_total"],
                           "中文社区卡不应被通识卡的偏低分数拖累")
        self.assertIn("scarcity", json.loads(scored["cn_breakdown"]))

    def test_cn_story_does_not_crowd_global_slots(self):
        cfg = make_config()
        stories = [
            make_story(id=f"g{i}", score=70 + i, event_type="rules") for i in range(4)
        ] + [make_story(id="c1", score=99, impact_scope="cn_community")]
        slots = select_slots(stories, cfg, set(), [])
        ids = [s["id"] for s in slots["general"]]
        self.assertNotIn("c1", ids)
        self.assertEqual(len(ids), 4)

    def test_mini_does_not_crowd_out_news(self):
        cfg = make_config()
        stories = [
            make_story(id=f"n{i}", score=65, event_type="business") for i in range(4)
        ] + [make_story(id="m1", score=95, event_type="mini")]
        slots = select_slots(stories, cfg, set(), [])
        ids = [s["id"] for s in slots["general"]]
        self.assertNotIn("m1", ids)
        self.assertEqual(len(ids), 4)

    def test_general_always_sorted_by_score(self):
        """多样性回填不得破坏「分数单调不增」的契约。"""
        cfg = make_config()
        stories = [
            make_story(id="a", title="Alpha battleforce revealed", score=70, event_type="rules"),
            make_story(id="b", title="Beta codex announced", score=66, event_type="rules"),
            make_story(id="c", title="Gamma novel release", score=64, event_type="novel"),
            make_story(id="d", title="Delta battleforce revealed", score=62, event_type="rules"),
        ]
        slots = select_slots(stories, cfg, set(), [])
        scores = [s["importance_total"] for s in slots["general"]]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestPushText(unittest.TestCase):
    def _slots(self, n: int) -> dict:
        return {
            "mode": "normal",
            "general": [make_story(id=f"s{i}", title_zh=f"标题{i}", score=90 - i) for i in range(n)],
            "product": [],
            "product_rows": [],
            "cn": None,
        }

    def test_respects_byte_limit(self):
        text = build_push_text(self._slots(4), "2026-11-17", "http://x", max_bytes=400)
        self.assertLessEqual(len(text.encode("utf-8")), 400)

    def test_contains_all_slots(self):
        text = build_push_text(self._slots(4), "2026-11-17", "http://x")
        for mark in "①②③④":
            self.assertIn(mark, text)
        self.assertIn("⑤", text)

    def test_sparse_notice(self):
        text = build_push_text(self._slots(2), "2026-11-17", "http://x")
        self.assertIn("仅 2 条达标", text)

    def test_product_rows_render_prices(self):
        slots = self._slots(0)
        slots["product_rows"] = [{"name": "Test Kit", "official": "£35", "expected": "¥380-460"}]
        text = build_push_text(slots, "2026-11-17", "http://x")
        self.assertIn("£35", text)
        self.assertIn("¥380-460", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
