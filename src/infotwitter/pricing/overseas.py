"""海外价：Element Games 抓取、名称匹配、版本/溢价守卫。"""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from .classify import classify_category
from .common import _valid_price, log, normalize_observed_at
from .money import get_rates, to_cny
from ..db import now_iso


_EXTRACT_JS = """() => {
    const out = [];
    document.querySelectorAll('a[href]').forEach(a => {
        const href = a.href || '';
        if (!href.includes('elementgames.co.uk')) return;
        if (href.includes('/search') || href.includes('/cart') || href.includes('/sign-in')) return;
        let node = a, depth = 0;
        while (node && depth < 4) {
            const txt = (node.innerText || '');
            const m = txt.match(/£\\s?(\\d+(?:\\.\\d{2})?)/g);
            if (m && m.length && txt.length < 400 && txt.length > 8 && (a.innerText || '').trim().length > 4) {
                const first = m[0].replace(/[^0-9.]/g, '');
                const second = m[1] ? m[1].replace(/[^0-9.]/g, '') : '';
                out.push({name: (a.innerText || '').trim(), rrp: first, sale: second, href: href});
                return;
            }
            node = node.parentElement; depth++;
        }
    });
    return out;
}"""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _similarity(a: str, b: str) -> float:
    """Jaccard（交/并），但先去掉品牌/编号这类噪声词。

    否则正确匹配会被前缀稀释掉：
      "COMBAT PATROL: BATTLEZONE" vs "WARHAMMER 40.000: COMBAT PATROL: BATTLEZONE"
      → 朴素 Jaccard 只有 0.50，被阈值 0.7 拒掉；去掉 warhammer/40/000 后是 1.00。
    反过来，"VOID WAR BASES" vs "BOARDING ACTIONS: VOID WAR BASES" 去噪后是 3/5=0.60，
    仍然会被拒——这正是想要的（它们确实是两个商品）。
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# 品牌/编号噪声：几乎每个商品名都有，参与相似度只会稀释真正的型号词
_MATCH_NOISE = frozenset({
    "warhammer", "40k", "40000", "400000", "30k", "aos", "sigmund",
    "games", "workshop", "citadel", "the", "of", "a", "an", "and",
})


def _tokens(text: str) -> set[str]:
    tokens = {t for t in _norm(text).split() if t and t not in _MATCH_NOISE}
    # 纯数字（40.000 拆出来的 40 / 000）也去掉
    return {t for t in tokens if not t.isdigit()}


_NOISE = re.compile(r"\b(warhammer|40000|40k|adeptus|astartes|games workshop)\b", re.I)
_LANG = re.compile(r"\((chn|eng|jp|chinese|english)\)", re.I)


def clean_query(name: str) -> str:
    """把 GW 商品名转成适合 Element Games 搜索的查询。"""
    q = _LANG.sub(" ", (name or "").lower())
    q = re.sub(r"[^a-z0-9 ]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def query_variants(name: str) -> list[str]:
    """由长到短生成多个查询，前一个匹配不到就用下一个。"""
    full = clean_query(name)
    short = re.sub(r"\s+", " ", _NOISE.sub(" ", full)).strip()
    tokens = short.split()
    candidates = [full, short]
    if len(tokens) > 3:
        candidates.append(" ".join(tokens[-3:]))
    if len(tokens) > 2:
        candidates.append(" ".join(tokens[-2:]))
    seen, out = set(), []
    for c in candidates:
        c = c.strip()
        if c and c not in seen and len(c) >= 4:
            seen.add(c)
            out.append(c)
    return out


def fetch_element_games(cfg: Any, items: list[tuple[str, str]], max_items: int = 12,
                        min_score: float = 0.7) -> dict[str, dict[str, Any]]:
    """按商品名在 Element Games 搜索并匹配，返回 {product_id: {price, url, name}}。"""
    if not items:
        return {}
    from playwright.sync_api import sync_playwright
    import urllib.parse

    results: dict[str, dict[str, Any]] = {}
    targets = items[:max_items]
    log.info("海外价：抓取 %d 个商品（Element Games）", len(targets))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": cfg.proxy} if cfg.proxy else None,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(user_agent=cfg.user_agent, locale="en-GB",
                                      viewport={"width": 1440, "height": 1200})
            page = ctx.new_page()
            for idx, (pid, name) in enumerate(targets, 1):
                best, best_score = None, 0.0
                for query in query_variants(name):
                    try:
                        page.goto("https://elementgames.co.uk/search?q=" + urllib.parse.quote(query),
                                  wait_until="domcontentloaded", timeout=35000)
                        page.wait_for_timeout(3000)
                        cards = page.evaluate(_EXTRACT_JS) or []
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Element 搜索失败 %s(%s): %s", pid, query, str(exc)[:50])
                        continue
                    for card in cards:
                        score = _similarity(name, card.get("name") or "")
                        if score > best_score:
                            best, best_score = card, score
                    # 只有"非常好"的匹配才提前收工；以前一旦达到阈值就 break，
                    # 结果是第一个变体的弱匹配挡住了后面更精确的查询（P-04）
                    if best_score >= max(min_score, 0.85):
                        break
                    time.sleep(1.5)

                if best and best_score >= min_score:
                    try:
                        rrp = float(best["rrp"])
                    except (TypeError, ValueError):
                        continue
                    results[pid] = {
                        "price": rrp,
                        "sale": float(best["sale"]) if best.get("sale") else None,
                        "url": best.get("href"),
                        "matched": best.get("name"),
                        "score": round(best_score, 2),
                    }
                    log.info("海外价 %s -> £%.2f（匹配度 %.2f）", pid, rrp, best_score)
                else:
                    log.debug("海外价未匹配 %s（最高分 %.2f）", pid, best_score)
                time.sleep(2.0)
                if idx % 5 == 0:
                    log.info("海外价进度 %d/%d，成功 %d", idx, len(targets), len(results))
        finally:
            browser.close()
    return results


MARKUP_MIN, MARKUP_MAX = 0.7, 2.5

# 版本/语言标记：中英文版、限定版、日文版价格不同，匹配时必须一致
EDITION_TOKENS = (
    "chinese", "chn", "eng", "english", "english-language",
    "jp", "jpn", "japanese", "limited", "collector", "collectors",
    "special", "deluxe", "anniversary",
)


def edition_signature(name: str) -> frozenset[str]:
    low = _norm(name)
    return frozenset(t for t in EDITION_TOKENS if re.search(rf"\b{t}\b", low))


def _category_family(name: str) -> str:
    """品类族：涂料/地形/书/规则书/成军包/配件与模型都不能互相匹配。

    以前把 codex/book/boxed 全归成 mini，于是「Codex: Adeptus Custodes」可以
    匹配到单个模型商品上（P-03）。
    """
    cat = classify_category(name)
    return {
        "paint": "paint",
        "terrain": "terrain",
        "book": "book",
        "codex": "codex",
        "boxed": "boxed",
        "accessory": "accessory",
    }.get(cat, "mini")


def save_overseas_points(conn: Any, cfg: Any, results: dict[str, dict[str, Any]],
                         observed_at: str | None = None) -> int:
    """写入海外价。按「日期」去重，重复抓取只更新当天记录。

    两道质量守卫（核查报告 BUG-20）：
    1. 中文/英文版、限定版必须与目标商品一致 —— 否则中文本会被匹配到英文版价格；
    2. 溢价率异常（相对国内官店）直接判 low，并在读取时被排除，不再当事实展示。
    """
    observed = normalize_observed_at(observed_at) or now_iso()[:10]
    rates = get_rates(cfg)
    saved = 0
    skipped = 0
    no_match = 0
    with conn:
        for pid, data in results.items():
            row = conn.execute("SELECT name_en, category FROM products WHERE id=?", (pid,)).fetchone()
            target_name = (row["name_en"] if row else "") or ""
            matched = str(data.get("matched") or "").strip()

            # 没有匹配名就无法校验版本/品类，宁可不写（P-03）
            if not matched or not target_name:
                no_match += 1
                continue
            if edition_signature(target_name) != edition_signature(matched):
                skipped += 1
                log.info("海外价跳过 %s：版本不一致（%s ← %s）", pid, target_name[:30], matched[:30])
                continue
            if _category_family(target_name) != _category_family(matched):
                skipped += 1
                log.info("海外价跳过 %s：品类不一致（%s ← %s）", pid, target_name[:30], matched[:30])
                continue

            price = data.get("price")
            if not _valid_price(price):
                skipped += 1
                log.warning("海外价跳过 %s：价格非法（%r）", pid, price)
                continue
            currency = str(data.get("currency") or "GBP").upper()
            cny = to_cny(float(price), currency, rates)
            if cny is None:
                skipped += 1
                log.warning("海外价跳过 %s：没有 %s 的汇率", pid, currency)
                continue

            parts = [f"匹配「{matched}」"]   # 存完整匹配名，purge 才能正确反查（P-13）
            if data.get("sale"):
                parts.append(f"折后 {currency} {data['sale']:.2f}")

            confidence = "high" if data.get("score", 0) >= 0.8 else "medium"
            # 用国内官店价反查：溢价率异常说明多半匹配错了商品
            cn_row = conn.execute(
                "SELECT price_cny FROM price_points "
                "WHERE product_id=? AND channel='cn_official' ORDER BY observed_at DESC LIMIT 1",
                (pid,),
            ).fetchone()
            if cn_row and cn_row["price_cny"] and cny:
                markup = cn_row["price_cny"] / cny
                if not (MARKUP_MIN <= markup <= MARKUP_MAX):
                    confidence = "low"
                    parts.append(f"溢价 {markup:.1f}× 异常，疑似匹配错误")

            conn.execute(
                "INSERT OR REPLACE INTO price_points "
                "(product_id, channel, platform, price, currency, price_cny, url, observed_at, confidence, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, "gw_uk", "Element Games", float(price), currency, cny,
                 data.get("url"), observed, confidence, "，".join(parts)),
            )
            saved += 1
    if no_match:
        log.warning("海外价有 %d 条没有匹配名，已跳过（无法校验版本/品类）", no_match)
    if skipped:
        log.warning("海外价因版本/品类/价格问题跳过 %d 条（宁缺毋滥）", skipped)
    return saved
