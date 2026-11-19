"""价格摘要：多渠道路由、最低价、趋势、槽 5 展示行。"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any
from .common import _usable, _weakest
from .money import get_rates


def latest_points(conn: Any, product_id: str) -> dict[str, list[dict[str, Any]]]:
    """取每个渠道最近一次的观测（同渠道可能有多个平台）。"""
    rows = conn.execute(
        """
        SELECT channel, platform, price, currency, price_cny, url, observed_at, confidence
        FROM price_points
        WHERE product_id=?
        ORDER BY observed_at DESC
        """,
        (product_id,),
    ).fetchall()
    by_channel: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["channel"], r["platform"] or "")
        if key in seen:
            continue
        seen.add(key)
        by_channel.setdefault(r["channel"], []).append(dict(r))
    return by_channel


def build_summary(
    conn: Any,
    cfg: Any,
    limit: int = 20,
    line: str | None = None,
    status: str | None = None,
    rotate_seed: int = 0,
) -> list[dict[str, Any]]:
    """生成价格摘要：海外价 / 国内官店价 / 国内价 / 最低价及平台。

    rotate_seed 用于每日轮换：所有商品的 updated_at 往往是同一天，只按它排序
    会让槽 5 每天输出同一批商品。带上有信号的商品仍然排在最前，
    其余同样"只有官店价"的商品按种子轮换（价格模块有效性）。
    """
    rates = get_rates(cfg)
    sql = (
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM price_points WHERE product_id=p.id AND channel='gw_uk') AS uk_count, "
        "(SELECT COUNT(*) FROM price_points WHERE product_id=p.id AND channel IN ('taobao','xianyu')) AS street_count "
        "FROM products p"
    )
    conds, params = [], []
    if line:
        conds.append("p.product_line=?")
        params.append(line)
    if status:
        conds.append("p.status=?")
        params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    # 先按"信息量"排（有海外价 / 有街价），同档内按种子轮换，最后才是 id。
    #
    # 轮换必须让种子**改变排序步长**，不能只是加一个常数：
    # 原来写 `rowid * C + seed`，种子每天 +1 等于给所有键同时 +1，
    # 取模后只是循环移位，顺序完全不变（实测连续 7 天 top5 一模一样）。
    # 现在把种子乘进步长，等价于每天换一个「跳跃间隔」，顺序才会真变。
    sql += (" ORDER BY (uk_count > 0) DESC, (street_count > 0) DESC, "
            "(ABS(p.rowid * (2654435761 + ? * 7919)) % 100003), p.updated_at DESC, p.id LIMIT ?")
    params.append(int(rotate_seed or 0))
    params.append(limit)

    out: list[dict[str, Any]] = []
    for p in conn.execute(sql, params).fetchall():
        channels = latest_points(conn, p["id"])

        def best(ch_names: tuple[str, ...]) -> dict[str, Any] | None:
            candidates = []
            for name in ch_names:
                # confidence=low 的观测不进摘要：它多半是匹配错的商品（BUG-20）
                candidates.extend(
                    pt for pt in channels.get(name, []) if (pt.get("confidence") or "medium") != "low"
                )
            if not candidates:
                return None
            return min(candidates, key=lambda x: x["price_cny"] or 1e12)

        overseas = best(("gw_uk", "gw_us", "gw_official"))
        cn_official = best(("cn_official",))
        cn_street = best(("taobao", "xianyu"))
        all_points = [pt for group in channels.values() for pt in group]
        # 最低价只在「国内可买」的渠道里找，海外价不参与（还要运费税费）
        # 渠道批发价（cn_trade）消费者买不到，不能算「最低价」
        # confidence=low 的观测连最低价都不能参与——否则一条 ¥50 的错配闲鱼帖
        # 会盖过真实的官店价（P-02）
        domestic = [
            pt for pt in all_points
            if pt["channel"] in ("cn_official", "taobao", "xianyu", "manual") and _usable(pt)
        ]
        distinct_platforms = {(pt["channel"], pt["platform"] or "") for pt in domestic}
        lowest = min(domestic, key=lambda x: x["price_cny"] or 1e12) if len(distinct_platforms) >= 2 else None

        out.append({
            "id": p["id"],
            "name": p["name_en"],
            "name_zh": p["name_zh"],
            "line": p["product_line"],
            "category": p["category"],
            "status": p["status"],
            "overseas": overseas,
            "cn_official": cn_official,
            "cn_street": cn_street,
            "lowest": lowest,
            "channels": channels,
        })
    return out


def expected_price(amount: float, currency: str, cfg: Any) -> dict[str, Any] | None:
    """国内预期价模型（设计 §9.3）：可解释的区间估计，不做黑箱。

        基准价 = 外币价 × 汇率 × (1 + 运费率) × (1 + 关税率)
        预期价 = 基准价 × 渠道加价率 × (1 ± spread)

    币种必须跟着价格一起传：以前写死 GBP，一旦写入 gw_us/gw_official 的美元价，
    就会把 USD 当 GBP 乘 9.2（T-05）。拿不到汇率或价格时返回 None——宁可不说，
    也不给假精度。
    """
    if not amount or amount <= 0 or cfg is None:
        return None
    cur = str(currency or "GBP").upper()
    rates = get_rates(cfg)
    rate = rates.get(cur)
    if not rate:
        return None
    freight = float(cfg.get("pricing.freight_rate", 0.08))
    duty = float(cfg.get("pricing.duty_rate", 0.09))
    markup = float(cfg.get("pricing.channel_markup", 1.12))
    spread = float(cfg.get("pricing.spread", 0.10))
    base = amount * rate * (1 + freight) * (1 + duty)
    mid = base * markup
    return {
        "base": round(base, 0),
        "low": round(mid * (1 - spread), 0),
        "mid": round(mid, 0),
        "high": round(mid * (1 + spread), 0),
        "currency": cur,
        "breakdown": (
            f"{cur} {amount:g} × {rate:g} × (1+{freight:g}) × (1+{duty:g}) = ¥{base:.0f} 基准价，"
            f"× 渠道加价 {markup:g} ± {int(spread * 100)}% = ¥{mid * (1 - spread):.0f}–{mid * (1 + spread):.0f}"
        ),
    }


def render_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "没有匹配的商品。"

    def price_of(pt: dict[str, Any] | None, show_platform: bool = False) -> str:
        if not pt:
            return "—"
        cny = pt["price_cny"]
        text = f"¥{cny:,.0f}" if cny else f"{pt['currency']}{pt['price']:g}"
        if show_platform and pt.get("platform"):
            text += f"({pt['platform'][:8]})"
        return text

    def overseas_text(pt: dict[str, Any] | None) -> str:
        if not pt:
            return "—"
        return f"{pt['currency']} {pt['price']:,.2f}"

    lines = []
    header = f"{'货号':<8}{'名称':<38}{'海外价':>14}{'国内官店':>10}{'国内价':>10}{'最低价及平台':>20}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in rows:
        name = (r.get("name_zh") or r.get("name") or "")[:36]
        line = (
            f"{r['id']:<8}{name:<38}"
            f"{overseas_text(r['overseas']):>14}"
            f"{price_of(r['cn_official']):>10}"
            f"{price_of(r['cn_street']):>10}"
            f"{price_of(r['lowest'], True):>20}"
        )
        lines.append(line)
    return "\n".join(lines)


def price_movements(conn: Any, days: int = 7) -> dict[tuple[str, str], float]:
    """§9.2 差分：窗口内「最新观测 vs 最早观测」的涨跌幅（%）。

    返回 {(product_id, channel): pct}，只保留有实际变化的。

    两个口径修正（P-02 / P-07）：
      - confidence=low 的观测不参与：它会凭空造出 -50% 这种假涨跌；
      - 同一渠道下若有多个平台（淘宝/闲鱼多家卖家），同日价格不可比，
        整条渠道跳过——只对「单一平台」的渠道算涨跌（官店、海外零售）。
    """
    import datetime as _dt

    since = (_dt.date.today() - _dt.timedelta(days=max(1, days))).isoformat()
    rows = conn.execute(
        "SELECT product_id, channel, platform, observed_at, price_cny FROM price_points "
        "WHERE observed_at >= ? AND price_cny IS NOT NULL "
        "AND COALESCE(confidence, 'medium') <> 'low' "
        "ORDER BY product_id, channel, platform, observed_at",
        (since,),
    ).fetchall()

    series: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
    for r in rows:
        key = (r["product_id"], r["channel"], r["platform"] or "")
        series.setdefault(key, []).append((str(r["observed_at"])[:10], float(r["price_cny"])))

    platforms: dict[tuple[str, str], set[str]] = {}
    first: dict[tuple[str, str], float] = {}
    last: dict[tuple[str, str], float] = {}
    for (pid, channel, platform), values in series.items():
        platforms.setdefault((pid, channel), set()).add(platform or "")
        ordered = [v for _, v in sorted(values, key=lambda x: x[0])]
        if len(ordered) < 2:
            continue
        first[(pid, channel)] = ordered[0]
        last[(pid, channel)] = ordered[-1]

    moves: dict[tuple[str, str], float] = {}
    for key, start in first.items():
        if len(platforms.get(key, ())) > 1:
            # 多卖家渠道：不同卖家的报价不是时间序列
            continue
        end = last[key]
        if start and end and abs(end - start) >= 0.01:
            moves[key] = round((end - start) / start * 100, 1)
    return moves


def price_coverage(conn: Any) -> dict[str, Any]:
    """价格数据的覆盖情况：让"看起来很空"这件事可见，而不是假装完整。"""
    total = conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    street = conn.execute(
        "SELECT COUNT(DISTINCT product_id) c FROM price_points"
        " WHERE channel IN ('taobao','xianyu','manual') AND COALESCE(confidence,'medium') <> 'low'"
    ).fetchone()["c"]
    overseas = conn.execute(
        "SELECT COUNT(DISTINCT product_id) c FROM price_points WHERE channel IN ('gw_uk','gw_us','gw_official')"
    ).fetchone()["c"]
    dates = [r["observed_at"] for r in conn.execute(
        "SELECT DISTINCT observed_at FROM price_points ORDER BY observed_at"
    )]
    return {
        "products": total,
        "with_street": street,
        "with_overseas": overseas,
        "dates": len(dates),
        "first_date": dates[0] if dates else "",
        "last_date": dates[-1] if dates else "",
    }


def build_slot5_rows(conn: Any, cfg: Any, limit: int = 5) -> list[dict[str, Any]]:
    """槽 5 展示行：优先本周新商品，其次最近更新的。

    字段语义必须诚实（核查报告 BUG-19）：
      official   = 海外零售价（观测值）
      cn_official= 国内官店建议零售价（观测值）
      expected   = 国内预期价（模型推算，仅在无官店观测时给出）
      observed   = 国内最低价（≥2 个渠道才有）
    三者不能用一个「→」连起来，那会让读者以为是换算结果。
    """
    import datetime as _dt

    today = _dt.date.today()
    # 每天换一批「只有官店价」的商品，避免槽 5 永远长一个样
    seed = int(today.strftime("%Y%m%d"))

    rows = build_summary(conn, cfg, limit=limit, status="preorder", rotate_seed=seed)
    if not rows:
        rows = build_summary(conn, cfg, limit=limit, status="new", rotate_seed=seed)
    if not rows:
        rows = build_summary(conn, cfg, limit=limit, rotate_seed=seed)

    moves = price_movements(conn, int(cfg.get("pricing.movement_days", 7)))

    out = []
    for r in rows:
        overseas = r["overseas"]
        official = r["cn_official"]
        low = r["lowest"]

        # 数据日期：取该商品最新一次观测，读者据此判断新鲜度
        observed_dates = [
            str(pt.get("observed_at") or "")[:10]
            for group in r["channels"].values() for pt in group
        ]
        data_date = max([d for d in observed_dates if d], default="")

        model = None
        if overseas and not official:
            model = expected_price(
                float(overseas["price"]), str(overseas.get("currency") or "GBP"), cfg
            )

        # 涨跌：优先看国内官店，其次看海外价
        change = moves.get((r["id"], "cn_official"))
        if change is None:
            change = moves.get((r["id"], "gw_uk"))
        stale_days = 0
        if data_date:
            try:
                stale_days = (today - _dt.date.fromisoformat(data_date)).days
            except ValueError:
                stale_days = 0

        # 行置信度取展示值里最弱的一个：不能用官店价的 high 给闲鱼价背书（P-02）
        row_confidence = _weakest([overseas, official, low])

        row = {
            "id": r["id"],
            "name": r["name"],
            "name_zh": r.get("name_zh"),
            # 只放行 http(s)：库里可能有手工 CSV 写进来的 javascript: 链接（P-15）
            "url": _safe_url((overseas or official or low or {}).get("url")),
            "official": (
                f"{overseas['currency']} {overseas['price']:g}" if overseas else ""
            ),
            "cn_official": (
                f"¥{official['price_cny']:.0f}" if official and official.get("price_cny") else ""
            ),
            "expected": (
                f"¥{model['low']:.0f}–{model['high']:.0f}" if model else ""
            ),
            "expected_note": (model or {}).get("breakdown", ""),
            "observed": (
                f"¥{low['price_cny']:.0f} {low.get('platform') or ''}".strip()
                if low and low.get("price_cny") else ""
            ),
            "change_pct": change,
            "change": (f"{change:+.0f}%" if change is not None else ""),
            "data_date": data_date,
            "stale_days": stale_days,
            "confidence": row_confidence,
            "line": r.get("line"),
        }
        out.append(row)
    return out


def _safe_url(url: Any) -> str:
    """只放行 http(s)，其余（javascript:、data:、空值）一律丢弃。"""
    text = str(url or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else ""
