"""评分回放的单元测试。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infotwitter.replay import (  # noqa: E402
    DIMENSIONS,
    Row,
    dimension_stats,
    parse_weights,
    recompute,
    run_replay,
)

BASE = {"authority": 20, "magnitude": 25, "buzz": 20, "actionability": 15,
        "freshness": 10, "interest": 10}


def make_row(scores: dict[str, float], cat: float = 1.0, total: float = 50.0) -> Row:
    return Row(
        story_id="x", title="t",
        scores={d: scores.get(d, 3.0) for d in DIMENSIONS},
        category_weight=cat, old_total=total, new_total=0.0,
    )


class TestWeights(unittest.TestCase):
    def test_parse_merges_into_base(self):
        w = parse_weights("buzz=8", BASE)
        self.assertEqual(w["buzz"], 8)
        self.assertEqual(w["authority"], 20)

    def test_parse_rejects_unknown_dimension(self):
        with self.assertRaises(ValueError):
            parse_weights("foo=1", BASE)

    def test_parse_rejects_malformed(self):
        with self.assertRaises(ValueError):
            parse_weights("buzz", BASE)

    def test_none_returns_base(self):
        self.assertEqual(parse_weights(None, BASE), BASE)


class TestRecompute(unittest.TestCase):
    def test_all_fives_hits_weight_sum(self):
        row = make_row({d: 5.0 for d in DIMENSIONS})
        self.assertAlmostEqual(recompute(row, BASE), 100.0, places=1)

    def test_all_zeros_is_zero(self):
        row = make_row({d: 0.0 for d in DIMENSIONS})
        self.assertEqual(recompute(row, BASE), 0.0)

    def test_category_weight_applied(self):
        row = make_row({d: 4.0 for d in DIMENSIONS}, cat=0.6)
        full = make_row({d: 4.0 for d in DIMENSIONS}, cat=1.0)
        self.assertLess(recompute(row, BASE), recompute(full, BASE))

    def test_matches_stored_formula(self):
        """回放公式必须与 pipeline.score_story 一致，否则调参结论不可信。"""
        scores = {"authority": 5.0, "magnitude": 3.0, "buzz": 1.0,
                  "actionability": 3.0, "freshness": 5.0, "interest": 2.5}
        expected = sum(BASE[d] * scores[d] / 5 for d in DIMENSIONS)
        self.assertAlmostEqual(recompute(make_row(scores), BASE), round(expected, 1), places=1)


class TestDimensionStats(unittest.TestCase):
    def test_constant_dimension_has_zero_power(self):
        rows = [make_row({d: 3.0 for d in DIMENSIONS}) for _ in range(5)]
        stats = dimension_stats(rows, BASE)
        for s in stats:
            self.assertAlmostEqual(s["std"], 0.0, places=2)
            self.assertAlmostEqual(s["power"], 0.0, places=2)

    def test_varying_dimension_ranks_first(self):
        rows = []
        for i in range(5):
            rows.append(make_row({"magnitude": float(i), "authority": 3.0,
                                  "buzz": 3.0, "actionability": 3.0,
                                  "freshness": 3.0, "interest": 3.0}))
        stats = dimension_stats(rows, BASE)
        self.assertEqual(stats[0]["dim"], "magnitude")


class TestRunReplay(unittest.TestCase):
    def _conn(self, rows):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE stories (id TEXT, title_zh TEXT, canonical_title TEXT,"
            " importance_total REAL, score_breakdown TEXT)"
        )
        for i, scores in enumerate(rows):
            b = {**scores, "category_weight": 1.0}
            conn.execute(
                "INSERT INTO stories VALUES (?,?,?,?,?)",
                (f"s{i}", f"标题{i}", "", 0.0, json.dumps(b)),
            )
        return conn

    def test_empty_db_message(self):
        conn = self._conn([])
        self.assertIn("没有可回放", run_replay(conn, BASE, BASE))

    def test_reports_stability(self):
        rows = [
            {"authority": 5.0, "magnitude": 5.0, "buzz": 1.0,
             "actionability": 1.0, "freshness": 1.0, "interest": 1.0},
            {"authority": 1.0, "magnitude": 1.0, "buzz": 1.0,
             "actionability": 1.0, "freshness": 1.0, "interest": 1.0},
        ]
        conn = self._conn(rows)
        out = run_replay(conn, BASE, BASE)
        self.assertIn("Top4 重合", out)
        self.assertIn("各维度区分度", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
