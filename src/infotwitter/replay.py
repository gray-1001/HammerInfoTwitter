"""评分回放：用已存储的分项得分重算排名，无需重新调用 LLM。

存在 `stories.score_breakdown` 里的六个子分是评分卡 v1 的原始判断，
换一组权重只是重新加权，不改变抽取结果——所以调参可以零成本反复试。

用法：
    python run.py replay                          # 看当前权重下 Top10 + 各维度区分度
    python run.py replay --top 15
    python run.py replay --weights buzz=8,magnitude=32,authority=18
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

DIMENSIONS = ["authority", "magnitude", "buzz", "actionability", "freshness", "interest"]
DIM_LABEL = {
    "authority": "来源权威",
    "magnitude": "事件量级",
    "buzz": "传播热度",
    "actionability": "可行动性",
    "freshness": "时效",
    "interest": "个人相关",
}


@dataclass
class Row:
    story_id: str
    title: str
    scores: dict[str, float]
    category_weight: float
    old_total: float
    new_total: float
    rank_old: int = 0
    rank_new: int = 0


def parse_weights(spec: str | None, base: dict[str, float]) -> dict[str, float]:
    weights = dict(base)
    if not spec:
        return weights
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"权重格式应为 key=value，收到：{part}")
        key, _, value = part.partition("=")
        key = key.strip()
        if key not in DIMENSIONS:
            raise ValueError(f"未知维度 {key}，可用：{', '.join(DIMENSIONS)}")
        weights[key] = float(value)
    return weights


def recompute(row: Row, weights: dict[str, float]) -> float:
    # 缺维度用 0 而不是抛 KeyError：自定义权重时不该整条回放崩掉（BUG-48）
    raw = sum(weights.get(d, 0.0) * row.scores.get(d, 0.0) / 5 for d in DIMENSIONS)
    return round(raw * row.category_weight, 1)


def load_rows(conn: sqlite3.Connection) -> list[Row]:
    rows = []
    for r in conn.execute(
        "SELECT id, title_zh, canonical_title, importance_total, score_breakdown "
        "FROM stories WHERE score_breakdown IS NOT NULL"
    ):
        try:
            b = json.loads(r["score_breakdown"])
        except (TypeError, json.JSONDecodeError):
            continue
        rows.append(
            Row(
                story_id=r["id"],
                title=(r["title_zh"] or r["canonical_title"] or "")[:44],
                scores={d: float(b.get(d, 0.0)) for d in DIMENSIONS},
                category_weight=float(b.get("category_weight", 1.0)),
                old_total=float(r["importance_total"] or 0),
                new_total=0.0,
            )
        )
    return rows


def dimension_stats(rows: list[Row], weights: dict[str, float]) -> list[dict[str, Any]]:
    """每个维度的区分度 = 标准差 × 权重。越高说明这个维度越能拉开差距。"""
    stats = []
    for d in DIMENSIONS:
        values = [r.scores[d] for r in rows]
        n = len(values)
        if n == 0:
            continue
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / n
        std = var ** 0.5
        stats.append({
            "dim": d,
            "label": DIM_LABEL[d],
            "mean": round(mean, 2),
            "std": round(std, 2),
            "weight": weights.get(d, 0.0),
            "power": round(std * weights.get(d, 0.0) / 5, 2),
        })
    return sorted(stats, key=lambda x: x["power"], reverse=True)


def run_replay(
    conn: sqlite3.Connection,
    base_weights: dict[str, float],
    new_weights: dict[str, float],
    top: int = 10,
) -> str:
    rows = load_rows(conn)
    if not rows:
        return "库中没有可回放的事件（score_breakdown 为空）"

    for r in rows:
        r.new_total = recompute(r, new_weights)

    old_sorted = sorted(rows, key=lambda r: r.old_total, reverse=True)
    new_sorted = sorted(rows, key=lambda r: r.new_total, reverse=True)
    for i, r in enumerate(old_sorted, 1):
        r.rank_old = i
    for i, r in enumerate(new_sorted, 1):
        r.rank_new = i

    lines: list[str] = []
    lines.append(f"事件总数 {len(rows)}    展示 Top{top}\n")

    lines.append("=== 各维度区分度（标准差 × 权重，越高越能拉开差距）===")
    lines.append(f"{'维度':<8}{'均值':>7}{'标准差':>8}{'权重':>7}{'贡献':>8}")
    for s in dimension_stats(rows, new_weights):
        flag = "  ← 几乎不区分" if s["std"] < 1.0 else ""
        lines.append(
            f"{s['label']:<8}{s['mean']:>7}{s['std']:>8}{s['weight']:>7}{s['power']:>8}{flag}"
        )

    lines.append("")
    lines.append("=== 新权重下的 Top%d（对比旧排名）===" % top)
    for i, r in enumerate(new_sorted[:top], 1):
        delta = r.rank_old - r.rank_new
        arrow = "=" if delta == 0 else (f"↑{delta}" if delta > 0 else f"↓{-delta}")
        lines.append(
            f"{i:>2}. {arrow:<4} {r.new_total:>5} (旧 {r.old_total:>5})  {r.title}"
        )

    moved = sum(1 for r in rows if r.rank_old != r.rank_new)
    top4_old = {r.story_id for r in old_sorted[:4]}
    top4_new = {r.story_id for r in new_sorted[:4]}
    overlap = len(top4_old & top4_new)
    lines.append("")
    lines.append(
        f"排名变动 {moved}/{len(rows)} 条；Top4 重合 {overlap}/4"
        + ("（排名稳定，改权重不影响今天的结论）" if overlap == 4 else "（新权重的 Top4 有明显变化）")
    )
    return "\n".join(lines)
