"""淘宝 / 闲鱼价格采集（持久化登录态）。

登录方式：`run.py price --login xianyu` 会打开一个可见浏览器，你扫码一次，
cookie 落在 data/browser_profile/<site>/ 下，之后无头运行自动复用。

为什么不用 cookie 文件：Chrome 127+ 对 Cookie 启用 App-Bound 加密，
非管理员权限无法解密，`browser_cookie3` 直接抛 RequiresAdminError（已实测）。
所以走「自己维护一个浏览器 profile」的路线，和 ai-goofish 的做法一致。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROFILE_ROOT = Path(__file__).resolve().parents[2] / "data" / "browser_profile"

SITES = {
    "xianyu": {
        "name": "闲鱼",
        "home": "https://www.goofish.com",
        "search": "https://www.goofish.com/search?q={q}",
        # 登录弹窗出现时页面一定有这些文案
        "login_check": ["手机扫码安全登录", "短信登录", "请先登录", "扫码登录"],
        # 真正的用户 cookie：unb 是用户 ID，cookie2 是登录票据。
        # 注意 _m_h5_tk 匿名访问也有，绝不能当作登录标志（实测误判过一次）。
        "cookie_hint": ["unb"],   # 用户 ID，只在真正登录后出现
        "lang": "zh-CN",
    },
    "taobao": {
        "name": "淘宝",
        "home": "https://www.taobao.com",
        "search": "https://s.taobao.com/search?q={q}",
        "login_check": ["密码登录", "扫码登录", "请登录", "亲，请登录"],
        "cookie_hint": ["unb"],   # 用户 ID，只在真正登录后出现
        "lang": "zh-CN",
    },
}

PRICE_RE = re.compile(r"[¥￥]\s?([\d,]+(?:\.\d+)?)")
NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


def _profile_dir(site: str) -> Path:
    path = PROFILE_ROOT / site
    path.mkdir(parents=True, exist_ok=True)
    return path


def _launch(p, site: str, proxy: str | None, ua: str, headless: bool):
    """启动持久化上下文，并回填会话 cookie。

    淘宝/闲鱼的登录票据（cookie2、_tb_token_ 等）是 session cookie（expires=-1），
    关闭浏览器时不会写进 profile 数据库，只会留在 storage_state 里。
    不回填的话，重新打开就是未登录状态（实测踩过）。
    """
    meta = SITES[site]
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(_profile_dir(site)),
        headless=headless,
        proxy={"server": proxy} if proxy else None,
        user_agent=ua,
        locale=meta["lang"],
        viewport={"width": 1440, "height": 1000},
        args=["--disable-blink-features=AutomationControlled"],
    )

    state_path = _profile_dir(site) / "state.json"
    if state_path.exists():
        try:
            import json

            state = json.loads(state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies") or []
            if cookies:
                ctx.add_cookies(cookies)
                log.debug("%s 回填会话 cookie %d 条", meta["name"], len(cookies))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 回填 cookie 失败：%s", meta["name"], exc)
    return ctx


def _has_login_cookie(ctx: Any, site: str) -> bool:
    hints = SITES[site]["cookie_hint"]
    names = {c["name"] for c in ctx.cookies()}
    return any(h in names for h in hints)


def _page_needs_login(text: str, site: str) -> bool:
    head = (text or "")[:800]
    return any(marker in head for marker in SITES[site]["login_check"])


def login(site: str, proxy: str | None, ua: str, wait_seconds: int = 240) -> bool:
    """打开可见浏览器让用户扫码，检测到登录态后保存并退出。"""
    from playwright.sync_api import sync_playwright

    meta = SITES[site]
    print(f"\n即将打开浏览器窗口。请用【{meta['name']}】App 扫描页面上的二维码完成登录。")
    print(f"最多等待 {wait_seconds} 秒；检测到登录后窗口会自动关闭。\n", flush=True)

    with sync_playwright() as p:
        ctx = _launch(p, site, proxy, ua, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(meta["home"], wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + wait_seconds
            last_report = 0
            while time.time() < deadline:
                page.wait_for_timeout(2500)
                try:
                    text = page.inner_text("body")[:1500]
                except Exception:  # noqa: BLE001
                    text = ""
                cookie_ok = _has_login_cookie(ctx, site)
                modal_gone = not _page_needs_login(text, site)
                # 二次校验：刷新首页，确认不再是未登录状态
                if cookie_ok and modal_gone:
                    try:
                        page.goto(meta["home"], wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2500)
                        text = page.inner_text("body")[:1500]
                        modal_gone = not _page_needs_login(text, site)
                    except Exception:  # noqa: BLE001
                        modal_gone = False

                if cookie_ok and modal_gone:
                    ctx.storage_state(path=str(_profile_dir(site) / "state.json"))
                    log.info("%s 登录态已保存（%d 条 cookie）", meta["name"], len(ctx.cookies()))
                    print(f"✅ {meta['name']} 登录成功（已拿到用户 cookie），登录态已保存", flush=True)
                    return True

                elapsed = int(time.time() - (deadline - wait_seconds))
                if elapsed - last_report >= 20:
                    last_report = elapsed
                    hint = "已检测到 cookie，等待弹窗关闭" if cookie_ok else "等待扫码…"
                    print(f"   [{elapsed:>3}s] {hint}", flush=True)
            print(f"❌ 超时：{wait_seconds} 秒内未检测到 {meta['name']} 登录态", flush=True)
            return False
        finally:
            ctx.close()


# 闲鱼卡片：a.feeds-item-wrap--xxxx，innerText 逐行排列：
#   标题 / 成色 / 品牌 / 规格 / ¥ / 365 / 4人想要 / 省份 / ...
_XIANYU_JS = """() => {
    const out = [];
    document.querySelectorAll('a[class*="feeds-item-wrap"]').forEach(a => {
        const txt = a.innerText || '';
        if (txt.length > 10) out.push({text: txt, href: a.href || ''});
    });
    return out;
}"""

# 淘宝卡片结构不同，给一套较宽松的兜底
_TAOBAO_JS = """() => {
    const out = [];
    document.querySelectorAll('[class*="doubleCardWrapper"], [class*="Card--"], a[href*="item.taobao"], a[href*="detail.tmall"]').forEach(el => {
        const txt = el.innerText || '';
        if (txt.length > 10 && /[¥￥]/.test(txt)) out.push({text: txt, href: el.href || ''});
    });
    return out;
}"""


def _parse_card_text(text: str) -> tuple[str, float] | None:
    """从卡片文本里解析 (标题, 价格)。"""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    if not lines:
        return None

    title = ""
    for ln in lines:
        if len(ln) >= 6 and "¥" not in ln and "想要" not in ln and "包邮" not in ln[:2]:
            title = ln
            break
    if not title:
        title = lines[0]

    price = None
    for i, ln in enumerate(lines):
        m = PRICE_RE.search(ln)
        if m:
            price = float(m.group(1).replace(",", ""))
            break
        if ln in ("¥", "￥") or ln.startswith("¥"):
            for nxt in lines[i + 1:i + 3]:
                m2 = NUM_RE.search(nxt)
                if m2:
                    price = float(m2.group(1).replace(",", ""))
                    break
            if price:
                break
    if not price:
        return None
    return title[:90], price


def search(
    site: str, cfg: Any, keyword: str, limit: int = 8, headless: bool = True,
    require: list[str] | None = None,
) -> list[dict[str, Any]]:
    """在指定站点搜索关键词，返回带价格的条目。

    require：标题里至少要命中其中一个词，否则丢弃。
    闲鱼在「没有搜索结果」时会回退成推荐流，而推荐流用的是同一套卡片 class，
    不过滤的话会抽到槟榔摊、连衣裙这种完全无关的东西（2026-11-18 实测）。
    """
    from playwright.sync_api import sync_playwright
    import urllib.parse

    meta = SITES[site]
    url = meta["search"].format(q=urllib.parse.quote(keyword))
    results: list[dict[str, Any]] = []
    js = _XIANYU_JS if site == "xianyu" else _TAOBAO_JS
    needles = [n for n in (require or []) if n]

    with sync_playwright() as p:
        ctx = _launch(p, site, cfg.proxy, cfg.user_agent, headless=headless)
        try:
            if not _has_login_cookie(ctx, site):
                log.warning(
                    "%s 未登录（缺少 unb cookie）。请重新运行：run.py price --login %s",
                    meta["name"], site,
                )
                return []
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # 结果区是异步渲染的，实测 10 秒左右稳定
            page.wait_for_timeout(10000)
            cards = page.evaluate(js) or []
            log.debug("%s 搜索「%s」拿到 %d 张卡片", meta["name"], keyword, len(cards))
            dropped_irrelevant = 0
            for c in cards:
                parsed = _parse_card_text(c.get("text") or "")
                if not parsed:
                    continue
                title, price = parsed
                if price <= 0:
                    continue
                if needles and not any(n in title for n in needles):
                    dropped_irrelevant += 1
                    continue
                results.append({
                    "title": title,
                    "price": price,
                    "url": c.get("href") or "",
                    "platform": meta["name"],
                    "raw": (c.get("text") or "").replace("\n", " | ")[:200],
                })
            if dropped_irrelevant:
                log.info("%s 丢弃 %d 条与关键词无关的卡片（多半是推荐流）",
                         meta["name"], dropped_irrelevant)
        finally:
            ctx.close()

    # 去重后按价格升序
    seen, uniq = set(), []
    for r in results:
        key = (r["title"][:40], r["price"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    # 先按价格升序再截断会把「最便宜的 N 条」的中位数当成市场价（BUG-32）。
    # 这里改成等距抽样，尽量保留整体分布。
    uniq.sort(key=lambda r: r["price"])
    if limit and len(uniq) > limit:
        step = len(uniq) / float(limit)
        uniq = [uniq[int(i * step)] for i in range(limit)]
    return uniq


def median_price(items: list[dict[str, Any]]) -> float | None:
    """对抽样结果取中位数。

    注意调用方负责抽样策略：先把整体分布等距抽样再取中位，
    不能先截「最便宜的 N 条」——那会系统性低估市场价（BUG-32）。
    """
    if not items:
        return None
    prices = sorted(i["price"] for i in items)
    n = len(prices)
    mid = n // 2
    return prices[mid] if n % 2 else round((prices[mid - 1] + prices[mid]) / 2, 2)
