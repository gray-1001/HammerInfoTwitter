# -*- coding: utf-8 -*-
"""金标准回归快照：同一批输入必须产出同一批 story。

三条断言，对应三条教训：
1. 同输入跑两次 → 完全一致（以前同一秒并列会退化成输入顺序）；
2. 打乱输入顺序 → 簇成员与分数不变（可复现）；
3. 与固化的 snapshot 一致（改聚类规则必须显式更新快照，而不是悄悄漂移）。

再叠加语义断言：只共享泛化实体的文档不得合并，同一事件必须合并。
"""
from __future__ import annotations

import json
import random
import sys
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import CST  # noqa: E402
from infotwitter.pipeline import cluster_docs, score_story  # noqa: E402
import golden_corpus  # noqa: E402

SNAPSHOT = Path(__file__).parent / "golden" / "snapshot.json"
REFRESH = False  # 需要更新快照时改 True 跑一次，再改回来并 review diff

# 固定「现在」：时效分依赖 now，不注入的话快照过一天就会漂，
# 那样的快照测不出回归，只会天天报警。
FIXED_NOW = datetime(2026, 11, 18, 20, 0, 0, tzinfo=CST)


def _run(docs: list[dict], analyses: dict[int, dict], cfg=None) -> list[dict]:
    """跑一遍聚类 + 评分，返回可比对的精简结果。

    成员用文档自身的 id（而不是列表下标）：打乱顺序之后下标会变，id 不会，
    这样「顺序无关」才是一个有意义的断言。
    """
    stories = cluster_docs(docs, analyses)
    if cfg is None:
        cfg = load_config()
    out = []
    for s in stories:
        s.update(score_story(s, cfg, {}, now=FIXED_NOW))
        out.append({
            "members": sorted(s["member_ids"]),
            "title": s["canonical_title"],
            "importance": round(float(s.get("importance_total") or 0), 1),
            "event_type": s.get("event_type"),
        })
    return sorted(out, key=lambda x: x["members"])


class GoldenClusterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.docs, self.analyses = golden_corpus.build()

    def test_same_input_same_output(self):
        a = _run(self.docs, self.analyses)
        b = _run(self.docs, self.analyses)
        self.assertEqual(a, b, "同输入两次聚类结果不一致（确定性被破坏）")

    def test_shuffled_input_same_clusters(self):
        base = _run(self.docs, self.analyses)
        order = list(range(len(self.docs)))
        random.Random(20261119).shuffle(order)
        docs2 = [self.docs[i] for i in order]
        analyses2 = {new: self.analyses[old] for new, old in enumerate(order)}
        shuffled = _run(docs2, analyses2)
        self.assertEqual(base, shuffled, "打乱输入顺序后聚类结果变了（可复现性被破坏）")

    def test_semantic_expectations(self):
        stories = cluster_docs(self.docs, self.analyses)
        by_group: dict[str, set[int]] = defaultdict(set)
        for idx, d in enumerate(self.docs):
            by_group[d["group"]].add(d["id"])
        # 每组独立成簇的次数
        for group, expected in golden_corpus.EXPECTED_STORIES.items():
            ids = by_group[group]
            hits = [s for s in stories if set(s["member_ids"]) & ids]
            self.assertEqual(
                len(hits), expected,
                f"{group} 期望 {expected} 个 story，实际 {len(hits)}："
                f"{[s['canonical_title'] for s in hits]}",
            )

    def test_generic_entities_never_merge(self):
        """只共享泛化实体的四条帖必须各自独立——这是上一轮事故的回归点。"""
        stories = cluster_docs(self.docs, self.analyses)
        generic_ids = {d["id"] for d in self.docs if d["group"] == "generic_only"}
        for s in stories:
            overlap = set(s["member_ids"]) & generic_ids
            self.assertLessEqual(len(overlap), 1,
                                 f"泛化实体导致的假合并：{s['canonical_title']}")

    def test_snapshot(self):
        got = _run(self.docs, self.analyses)
        if REFRESH or not SNAPSHOT.exists():
            SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT.write_text(
                json.dumps(got, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.skipTest("快照已生成")
        want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(got, want, "聚类结果与金标准快照不一致（确认是预期变更再刷新快照）")


if __name__ == "__main__":
    unittest.main()
