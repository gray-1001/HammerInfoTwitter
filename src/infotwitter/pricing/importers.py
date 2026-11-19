"""价格导入：GW 官方调价表（Excel）、人工快照（CSV）、运行台账与错配清扫。"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from .classify import _to_float, classify_category, classify_line
from .common import MANUAL_CHANNELS, OFFICIAL_CHANNELS, RESERVED_PLATFORMS, _valid_price, log, normalize_observed_at
from .money import get_rates, to_cny
from .overseas import _category_family, _similarity, edition_signature, save_overseas_points
from ..db import now_iso


def _excel_layout(rows: list[tuple]) -> tuple[int, dict[str, int]]:
    """从表头里认出 CNR/CNB 列，认不出就退回固定的 5/6/7/8。

    只读 New 列会让"没有 New 列"的调价表导入 0 个价格点却报成功（P-06）。
    """
    for i, r in enumerate(rows[:6]):
        texts = [str(c if c is not None else "").strip().lower() for c in r]
        if not any("cnr" in t or "cnb" in t for t in texts):
            continue
        cols: dict[str, int] = {}
        for j, t in enumerate(texts):
            if "new" in t and "cnr" in t:
                cols["new_cnr"] = j
            elif "new" in t and "cnb" in t:
                cols["new_cnb"] = j
            elif "cnr" in t and "cur_cnr" not in cols:
                cols["cur_cnr"] = j
            elif "cnb" in t and "cur_cnb" not in cols:
                cols["cur_cnb"] = j
        if cols:
            return i + 1, cols
    return 2, {"cur_cnr": 5, "cur_cnb": 6, "new_cnr": 7, "new_cnb": 8}


def import_gw_excel(conn: Any, path: Path, observed_at: str | None = None) -> dict[str, Any]:
    """导入 GW 官方调价表。

    表结构（表头之后）：SS Code | Product Code | Description | Barcode |
                        Qty | Current CNR | Current CNB | New CNR | New CNB
    同一 SS Code 可能有两行（不同内包装编码），按货号聚合。

    三种静默丢数的情况都处理了（P-06）：
      - 表里没有 New 列 / New 为空 → 回退用 Current 并写进 note；
      - 合并单元格导致续行货号为空 → 继承上一行；
      - 数字格式货号（`9901`）没有连字符 → 也接受。
    """
    from openpyxl import load_workbook

    # 只按「日期」记录：同一份调价表重复导入应覆盖而不是堆积重复价格点
    observed = normalize_observed_at(observed_at) or now_iso()[:10]
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    raw = list(ws.iter_rows(min_row=1, values_only=True))
    wb.close()

    start, cols = _excel_layout(raw)

    def cell(row: tuple, key: str) -> Any:
        idx = cols.get(key)
        return row[idx] if idx is not None and idx < len(row) else None

    grouped: dict[str, dict[str, Any]] = {}
    invalid = 0
    used_current = 0
    last_code = ""
    for r in raw[start:]:
        if not r:
            continue
        raw_code = r[0] if r else None
        ss_code = str(raw_code).strip() if raw_code is not None else ""
        if not ss_code and last_code:
            # 合并单元格：续行的 A 列为空，货号沿用上一行
            ss_code = last_code
        if not ss_code:
            continue
        # 39-13 这种带连字符的货号，以及 9901 这种纯数字货号
        if "-" not in ss_code and not re.fullmatch(r"\d{4,}", ss_code):
            continue
        last_code = ss_code
        name = (str(r[2]).strip() if len(r) > 2 and r[2] else "")
        if not name:
            continue
        g = grouped.setdefault(
            ss_code,
            {"name": name, "barcode": "", "codes": [],
             "cnr": None, "cnb": None, "prev_cnr": None, "prev_cnb": None,
             "from_current": False},
        )
        if len(r) > 1 and r[1]:
            g["codes"].append(str(r[1]).strip())
        if len(r) > 3 and r[3] and not g["barcode"]:
            g["barcode"] = str(r[3]).strip()

        new_cnr, new_cnb = _to_float(cell(r, "new_cnr")), _to_float(cell(r, "new_cnb"))
        prev_cnr, prev_cnb = _to_float(cell(r, "cur_cnr")), _to_float(cell(r, "cur_cnb"))
        # New 为空 → 这次没调价，按 Current 记（否则这个商品一个价格点都没有）
        if new_cnr is None:
            new_cnr = prev_cnr
            g["from_current"] = True
        if new_cnb is None:
            new_cnb = prev_cnb

        for key, value in (("cnr", new_cnr), ("cnb", new_cnb)):
            if value is None:
                continue
            if not _valid_price(value):
                invalid += 1
                continue
            g[key] = value
        if _valid_price(prev_cnr):
            g["prev_cnr"] = prev_cnr
        if _valid_price(prev_cnb):
            g["prev_cnb"] = prev_cnb
    if invalid:
        log.warning("调价表有 %d 个价格不是正数，已跳过（不写负价/零价）", invalid)

    products_added = products_updated = points = 0
    with conn:
        for code, g in grouped.items():
            exists = conn.execute("SELECT 1 FROM products WHERE id=?", (code,)).fetchone()
            conn.execute(
                """
                INSERT INTO products (id, name_en, barcode, product_codes, product_line,
                                      category, status, first_seen_at, updated_at)
                VALUES (?,?,?,?,?,?,'active',?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name_en=excluded.name_en, barcode=excluded.barcode,
                    product_codes=excluded.product_codes, product_line=excluded.product_line,
                    category=excluded.category, updated_at=excluded.updated_at
                """,
                (
                    code, g["name"], g["barcode"],
                    __import__("json").dumps(g["codes"], ensure_ascii=False),
                    classify_line(g["name"]), classify_category(g["name"]),
                    observed, observed,
                ),
            )
            if exists:
                products_updated += 1
            else:
                products_added += 1

            suffix = "（表内无新价，按现价记录）" if g.get("from_current") else ""
            if g["cnr"]:
                note = "官方建议零售价（调价表）" + suffix
                if g.get("prev_cnr") and abs(g["prev_cnr"] - g["cnr"]) > 0.01:
                    note += f"，调价前 ¥{g['prev_cnr']:.0f}"
                conn.execute(
                    "INSERT OR REPLACE INTO price_points "
                    "(product_id, channel, platform, price, currency, price_cny, observed_at, confidence, note) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (code, "cn_official", "GW China RRP", g["cnr"], "CNY", g["cnr"], observed, "high", note),
                )
                points += 1
            if g["cnb"]:
                note = "渠道批发价（调价表）" + suffix
                if g.get("prev_cnb") and abs(g["prev_cnb"] - g["cnb"]) > 0.01:
                    note += f"，调价前 ¥{g['prev_cnb']:.0f}"
                conn.execute(
                    "INSERT OR REPLACE INTO price_points "
                    "(product_id, channel, platform, price, currency, price_cny, observed_at, confidence, note) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (code, "cn_trade", "GW China trade", g["cnb"], "CNY", g["cnb"], observed, "high", note),
                )
                points += 1
    if used_current:
        log.info("调价表有 %d 个商品没有新价，已按现价记录", used_current)

    stats = {
        "rows": len(raw),
        "products": len(grouped),
        "added": products_added,
        "updated": products_updated,
        "price_points": points,
        "columns": cols,
        "invalid_prices": invalid,
    }
    log.info(
        "导入调价表：%d 行 -> %d 个商品（新增 %d / 更新 %d），%d 个价格点",
        stats["rows"], stats["products"], stats["added"], stats["updated"], stats["price_points"],
    )
    if points == 0:
        # 一个价格点都没写进去还报成功，后面就没人会发现（P-06）
        stats["error"] = "没有写入任何价格点，请检查表头与列位置"
        log.error("导入调价表失败：%s（识别到的列：%s）", stats["error"], cols)
    return stats


def _open_csv_text(path: Path) -> str:
    """读 CSV 文本：中文 Excel 默认按 GBK/GB18030 导出，只认 utf-8 会直接炸（P-12）。"""
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, f"无法识别 {path.name} 的编码（试过 utf-8-sig/gb18030/utf-16）")


def import_manual_csv(
    conn: Any, path: Path, cfg: Any = None, observed_at: str | None = None
) -> dict[str, Any]:
    """导入人工快照 CSV（设计 §5.4「国内渠道人工锚点」）。

    表头：sku_id,sku_name,channel,vendor,price,currency,in_stock,url
    - sku_id 命中 products.id 时直接挂上去（但名称对不上会拒绝，避免挂错商品）；
    - 否则按 sku_name 模糊匹配，要求足够长且唯一最优；
    - channel 只允许人工渠道，不能写官方渠道 / 占用系统平台名（P-01）；
    - 观测时间统一成日期，避免同一天重复入库造成假涨跌（P-05）。
    """
    import csv as _csv
    import io as _io

    observed = normalize_observed_at(observed_at) or now_iso()[:10]
    stats = {"rows": 0, "saved": 0, "unmatched": 0, "skipped": 0,
             "rejected_channel": 0, "rejected_price": 0, "no_rate": 0, "observed_at": observed}
    if not path.exists():
        return stats

    rates = get_rates(cfg)
    products = [
        (r["id"], r["name_en"], r["name_zh"])
        for r in conn.execute("SELECT id, name_en, name_zh FROM products")
    ]
    known = {p[0] for p in products}
    label_of = {p[0]: (p[2] or p[1] or "") for p in products}
    name_index = []
    for pid, en, zh in products:
        for label in (zh, en):
            if label:
                name_index.append((pid, str(label).strip().lower()))

    reader = _csv.DictReader(_io.StringIO(_open_csv_text(path)))
    with conn:
        for row in reader:
            stats["rows"] += 1
            channel = (row.get("channel") or "manual").strip().lower()
            vendor = (row.get("vendor") or "manual").strip()
            if channel in OFFICIAL_CHANNELS or channel not in MANUAL_CHANNELS:
                stats["rejected_channel"] += 1
                log.warning("人工快照拒绝第 %d 行：channel=%r 不是允许的人工渠道", stats["rows"], channel)
                continue
            if vendor.lower() in RESERVED_PLATFORMS:
                stats["rejected_channel"] += 1
                log.warning("人工快照拒绝第 %d 行：vendor=%r 是系统平台名", stats["rows"], vendor)
                continue

            price = _to_float(row.get("price"))
            if not _valid_price(price, low=1.0):
                stats["rejected_price"] += 1
                continue

            pid, reason = _resolve_manual_target(row, known, label_of, name_index)
            if not pid:
                stats[reason] = stats.get(reason, 0) + 1
                continue

            currency = (row.get("currency") or "CNY").strip().upper()
            if currency == "CNY":
                cny = price
            else:
                cny = to_cny(price, currency, rates)
                if cny is None:
                    # 汇率缺失时不能把外币当人民币写进去
                    stats["no_rate"] += 1
                    log.warning("人工快照跳过 %s：没有 %s 的汇率", pid, currency)
                    continue

            conn.execute(
                "INSERT OR REPLACE INTO price_points "
                "(product_id, channel, platform, price, currency, price_cny, url, observed_at,"
                " confidence, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, channel, vendor, price, currency, cny, row.get("url") or "",
                 observed, "medium", "人工快照"),
            )
            stats["saved"] += 1
    return stats


def _resolve_manual_target(
    row: dict[str, Any], known: set[str], label_of: dict[str, str],
    name_index: list[tuple[str, str]],
) -> tuple[str, str]:
    """定位这一行属于哪个商品。返回 (product_id, 未命中的原因键)。"""
    raw_id = (row.get("sku_id") or "").strip()
    name = (row.get("sku_name") or "").strip()
    if raw_id in known:
        # 货号对上了还要核对名称：否则一行的名称与货号错位时会把价格挂错商品
        if name and label_of.get(raw_id):
            if _similarity(name, label_of[raw_id]) < 0.34:
                return "", "unmatched"
        return raw_id, ""
    needle = name.lower()
    if len(needle) < 4:
        return "", "unmatched"
    scored: list[tuple[float, str]] = []
    for cand_pid, label in name_index:
        if needle in label or label in needle:
            scored.append((_similarity(needle, label), cand_pid))
    if not scored:
        return "", "unmatched"
    scored.sort(reverse=True)
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-9:
        # 两个候选打平 → 宁可放过，不要挂错
        return "", "unmatched"
    return scored[0][1], ""


def record_price_run(conn: Any, week: str, stats: dict[str, Any], status: str = "ok") -> None:
    """记录一次价格采集（price_runs 之前是空表，等于没有进度台账）。"""
    import json as _json

    with conn:
        conn.execute(
            "INSERT INTO price_runs (week, started_at, finished_at, stats, status) "
            "VALUES (?,?,?,?,?)",
            (week, now_iso(), now_iso(), _json.dumps(stats, ensure_ascii=False), status),
        )


def auto_refresh(conn: Any, cfg: Any) -> dict[str, Any]:
    """日更任务里的价格自动刷新（§9.2 的「每天抓一次」在本地能力内的落地）。

    能自动做的只有「人工快照 CSV 有更新就导入」——GW 调价表和海外价都需要
    外部动作（拿到新表 / 跑浏览器），不在无人值守链路里冒险触发。
    在这里把「做不了」变成可见的提示，而不是假装做过。
    """
    from ..config import ROOT

    cfg_rel = str(cfg.get("pricing.manual_csv", "data/price_snapshots.csv") or "")
    stats: dict[str, Any] = {"csv_imported": 0, "csv_rows": 0, "skipped": ""}
    if not cfg_rel:
        return stats

    path = ROOT / cfg_rel
    if not path.exists():
        stats["skipped"] = "no_csv"
        return stats

    mtime = path.stat().st_mtime
    # 只找「带 csv_mtime 的那条记录」：以前取最后一条 run，只要中间跑过一次
    # Excel 导入（那条没有 csv_mtime），基准就被清成 0，CSV 没变也会重导（A-08）
    last = conn.execute(
        "SELECT stats FROM price_runs WHERE stats LIKE '%csv_mtime%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_mtime = 0.0
    if last and last["stats"]:
        try:
            last_mtime = float(json.loads(last["stats"]).get("csv_mtime") or 0)
        except (ValueError, TypeError):
            last_mtime = 0.0
    if mtime <= last_mtime:
        stats["skipped"] = "unchanged"
        return stats

    result = import_manual_csv(conn, path, cfg)
    record_price_run(conn, _iso_week(), {**result, "csv_mtime": mtime})
    stats.update({"csv_imported": result.get("saved", 0), "csv_rows": result.get("rows", 0)})
    log.info("自动导入人工快照：%s 行，入库 %s 条", result.get("rows"), result.get("saved"))
    return stats


def _iso_week() -> str:
    import datetime as _dt

    year, week, _ = _dt.date.today().isocalendar()
    return f"{year}-W{week:02d}"


def latest_price_date(conn: Any) -> str:
    row = conn.execute("SELECT MAX(observed_at) d FROM price_points").fetchone()
    return str(row["d"] or "")[:10] if row else ""


def purge_mismatched_overseas(conn: Any) -> dict[str, Any]:
    """清掉历史上因宽松匹配写入的错配海外价（一次性维护命令）。

    判定标准与 save_overseas_points 的新守卫一致：note 里记录的匹配名
    与商品名的版本签名/品类族不一致，或置信度为 low。
    note 现在存的是**完整匹配名**，不再截断到 40 字，否则版本标记落在 40 字之后
    时会被误判成"版本不一致"而删掉正确价格（P-13）。
    """
    removed = []
    rows = conn.execute(
        "SELECT id, product_id, confidence, note FROM price_points WHERE channel='gw_uk'"
    ).fetchall()
    with conn:
        for r in rows:
            note = r["note"] or ""
            matched = ""
            if "匹配「" in note:
                matched = note.split("匹配「", 1)[1].split("」", 1)[0]
            prod = conn.execute(
                "SELECT name_en FROM products WHERE id=?", (r["product_id"],)
            ).fetchone()
            target = (prod["name_en"] if prod else "") or ""
            bad = (r["confidence"] or "") == "low"
            if not bad and matched and target:
                bad = (
                    edition_signature(target) != edition_signature(matched)
                    or _category_family(target) != _category_family(matched)
                )
            if bad:
                conn.execute("DELETE FROM price_points WHERE id=?", (r["id"],))
                removed.append({"product_id": r["product_id"], "matched": matched[:40],
                                "confidence": r["confidence"]})
    return {"checked": len(rows), "removed": len(removed), "detail": removed}
