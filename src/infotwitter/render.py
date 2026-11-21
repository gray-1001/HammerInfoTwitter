"""排版：微信推送文本（受 2048 字节限制）与 HTML 日报页。"""
from __future__ import annotations

import html as html_mod
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .db import CST
from .tokens import feedback_token

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
CIRCLED = "①②③④⑤⑥⑦⑧⑨"

# 推送里只允许 http/https，避免 javascript: 之类的链接进入 href
ALLOWED_URL_SCHEMES = ("http://", "https://")

EVENT_LABELS = {
    "edition": "新版本",
    "rules": "规则",
    "mini": "模型新品",
    "novel": "出版",
    "film": "影视",
    "game": "电子游戏",
    "business": "公司动态",
    "event": "赛事活动",
    "rumor": "传闻",
    "other": "综合",
}


def event_label(event_type: str | None) -> str:
    return EVENT_LABELS.get(str(event_type or "").lower(), "综合")


def safe_url(url: str | None) -> str:
    """只放行 http/https，其余返回空串（不渲染链接）。"""
    text = (url or "").strip()
    return text if text.lower().startswith(ALLOWED_URL_SCHEMES) else ""

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
# 模板里所有 href 都过这个过滤器：报告页在局域网上，一个被篡改的源就能塞
# javascript: 链接进来，读者点一下就执行（P2-3）
_env.filters["safe_url"] = safe_url


def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _shown_score(item: dict[str, Any]) -> float:
    '''展示分：二级补位（本周稍早）用衰减后的分数，与排序口径保持一致。'''
    decayed = item.get("_decayed")
    return float(decayed if decayed is not None else (item.get("importance_total") or 0))


SLOT_REASON_LABEL = {
    "rumor": "未证实",
    "backlog": "历史推荐",
    "aged": "本周稍早",   # 48h 之外、一周之内，48h 内容不足时补位
}

MODE_LABELS = {
    "normal": "正常",
    "aged_fill": "本周补位",
    "aged_only": "全为本周稍早",
    "rumor_fill": "传闻补位",
    "cn_fill": "中文补位",
    "backlog_fill": "历史补位",
    "backlog_only": "全为历史",
    "sparse": "内容偏少",
    "empty": "无内容",
}


MODEL_DIM_LABELS = {
    "authority": "来源权威",
    "magnitude": "事件量级",
    "buzz": "传播热度",
    "actionability": "可行动性",
    "freshness": "时效",
    "interest": "个人相关",
}

CN_DIM_LABELS = {
    "affinity": "中文社区影响面",
    "obtainability": "可获得性",
    "source_trust": "来源可信",
    "scarcity": "稀缺性",
    "freshness": "时效",
}


def _age_note(item: dict[str, Any], date: str) -> str:
    """文章发布日期比日报日期早 2 天以上时，标出来让读者自己判断新旧。

    选槽按「首次出现」分层（设计 §20），所以一篇 6 天前发布、今天才被发现
    的文章确实会进「今日」日报。不标日期会让读者以为是今天的新闻。
    """
    pub = str(item.get("published_at") or "")[:10]
    if len(pub) != 10 or len(str(date or "")) < 10:
        return ""
    try:
        days = (datetime.strptime(str(date)[:10], "%Y-%m-%d")
                - datetime.strptime(pub, "%Y-%m-%d")).days
    except ValueError:
        return ""
    if days < 2:
        return ""
    return f" · 发布于 {pub[5:]}"


def _item_block(index: int, item: dict[str, Any], level: int, summary_limit: int = 60,
                date: str = "") -> str:
    """level 0=完整 1=去 why 2=去摘要 3=仅标题

    summary_limit 按通道的字节预算决定，不再一律砍到 60 字（设计 §2.1 允许 ≤120 字）。
    """
    mark = CIRCLED[index] if index < len(CIRCLED) else f"{index + 1}."
    title = _clip(item.get("title_zh") or item.get("canonical_title") or "", 34)
    lines = [f"{mark} {title}"]
    if level <= 0 and item.get("why_matters"):
        lines.append(f"   {_clip(item['why_matters'], 40)}")
    if level <= 1 and item.get("summary"):
        lines.append(f"   {_clip(item['summary'], summary_limit)}")
    tag = f"   {item.get('authenticity', 'L2')}"
    conf = item.get("confidence") or ""
    if conf:
        tag += f" {conf}"
    tag += f" · {_shown_score(item)}分"
    # 只在需要提醒读者时标注；cn_fill 等内部补位原因不暴露给用户
    label = SLOT_REASON_LABEL.get(item.get("_slot_reason") or "")
    if label and level <= 2:
        tag += f" · {label}"
    if level <= 2:
        tag += _age_note(item, date)
        lines.append(tag)
    return "\n".join(lines)


def build_push_text(
    slots: dict[str, Any],
    date: str,
    report_url: str,
    max_bytes: int = 2048,
    allow_split: bool = False,
) -> str:
    """构造推送文本，超限时逐级降级。

    allow_split=True 时保留完整文本，交给会分段发送的通道处理。
    """
    header = f"【战锤日报 {date[5:]}】"
    general = slots.get("general") or []
    product = slots.get("product") or []
    cn = slots.get("cn")
    # 2048 字节（企微）只能容纳约 60 字摘要；PushPlus 的 2 万字额度没必要砍到 60
    summary_limit = 120 if max_bytes >= 8000 else 60

    def compose(level: int) -> str:
        parts = [header]
        if not general:
            parts.append("今日无高价值信息。")
        for i, item in enumerate(general):
            parts.append(_item_block(i, item, level, summary_limit, date))
        if 0 < len(general) < 4 and level <= 1:
            parts.append(f"（今日仅 {len(general)} 条达标，其余为低价值信息）")

        parts.append("")
        product_rows = slots.get("product_rows") or []
        if product_rows:
            as_of = max((r.get("data_date") or "" for r in product_rows), default="")
            moved = any(r.get("change_pct") is not None for r in product_rows)
            head = "⑤ 模型价格速览"
            if as_of:
                head += f"（数据日 {as_of}"
                head += "）" if moved else " · 暂无变动）"
            parts.append(head)
            for row in product_rows[:3]:
                line = f"   {_clip(row.get('name_zh') or row.get('name') or '', 24)}"
                if row.get("official"):
                    line += f" {row['official']}"
                if row.get("cn_official"):
                    line += f" / 国内 {row['cn_official']}"
                elif row.get("expected"):
                    line += f" / 预期 {row['expected']}"
                if row.get("observed"):
                    line += f" / 最低 {_clip(row['observed'], 18)}"
                if row.get("change"):
                    line += f"（{row['change']}）"
                parts.append(line)
            stale = max((int(r.get("stale_days") or 0) for r in product_rows), default=0)
            if stale > 7:
                parts.append(f"   ⚠ 价格数据已 {stale} 天未更新")
        elif product:
            # 与另外两处标题保持一致：台账以前记「本周模型快讯」，正文却是「模型价格速览」（P2-5）
            parts.append(f"⑤ 模型价格速览（{len(product)} 项）")
            for p in product[:3]:
                parts.append(f"   {_clip(p.get('title_zh') or p.get('canonical_title') or '', 32)}")
        elif slots.get("product_refs"):
            marks = "、".join(
                CIRCLED[i - 1] if 0 < i <= len(CIRCLED) else str(i)
                for i in slots["product_refs"]
            )
            parts.append(f"⑤ 模型价格：见 {marks}")
        else:
            parts.append("⑤ 价格数据暂缺（采集未运行或抓取失败）。")

        if cn:
            age = float(cn.get("age_days") or 0)
            parts.append("")
            parts.append(f"⑥ 【中文社区】{_clip(cn.get('title_zh') or cn.get('canonical_title') or '', 30)}")
            if age >= 1:
                parts.append(f"   （{age:.0f} 天前）")
            if level <= 1 and cn.get("why_matters"):
                parts.append(f"   {_clip(cn['why_matters'], 40)}")

        parts.append("")
        parts.append(f"完整版 → {report_url}")
        return "\n".join(parts)

    text = compose(0)
    for level in (1, 2, 3):
        if len(text.encode("utf-8")) <= max_bytes:
            break
        text = compose(level)

    if len(text.encode("utf-8")) > max_bytes:
        if allow_split:
            # 通道会自己分段，别在这里截断
            return text
        # 最后手段：硬截断，但必须保住「完整版」链接——设计 §10.1 的兜底
        footer = f"\n…（已截断）\n完整版 → {report_url}"
        budget = max_bytes - len(footer.encode("utf-8"))
        if budget < 40:
            # 上限小到连链接都放不下：退回纯截断，不能超限
            encoded = text.encode("utf-8")[: max(0, max_bytes - 20)]
            return encoded.decode("utf-8", "ignore") + "\n…（已截断）"
        encoded = text.encode("utf-8")[:budget]
        text = encoded.decode("utf-8", "ignore") + footer
    return text


def build_push_html(
    slots: dict[str, Any],
    date: str,
    report_url: str,
    max_bytes: int = 20000,
) -> str:
    """给 PushPlus 的 HTML 版推送。

    微信公众号对 CSS 支持有限，这里只用最保守的标签：
    <b> / <span style> / <br> / <hr> / <a>，避免 flex、grid、伪类。

    设计 §10.1 要求的「为什么重要」与 §8.4 要求的「未证实/历史推荐」徽章
    在 HTML 版必须与文本版一致，否则用户实际收到的版本会丢失这些信息。
    """
    general = slots.get("general") or []
    product = slots.get("product") or []
    product_rows = slots.get("product_rows") or []
    cn = slots.get("cn")
    summary_limit = 400

    def esc(text: str) -> str:
        # quote=True 才能防住 href="..." 的属性注入
        return html_mod.escape(text or "", quote=True)

    def link(url: str, label: str, size: int = 12) -> str:
        safe = safe_url(url)
        if not safe:
            return ""
        return (
            f'<div style="margin-top:4px"><a href="{esc(safe)}" '
            f'style="color:#2f6fb5;font-size:{size}px">{label}</a></div>'
        )

    parts: list[str] = []
    parts.append(
        f'<div style="font-size:15px;line-height:1.7;color:#222">'
        f'<b style="font-size:17px">战锤日报 · {date[5:]}</b>'
        f'<span style="color:#999;font-size:12px">　{MODE_LABELS.get(slots.get("mode", ""), "")}</span>'
        f"</div>"
    )

    for i, item in enumerate(general):
        mark = CIRCLED[i] if i < len(CIRCLED) else f"{i + 1}."
        title = esc(_clip(item.get("title_zh") or item.get("canonical_title") or "", 40))
        tier = item.get("authenticity", "L2")
        score = _shown_score(item)
        summary = esc(_clip(item.get("summary") or "", summary_limit))
        why = esc(_clip(item.get("why_matters") or "", 60))
        url = item.get("primary_url") or ""

        tier_color = {"L0": "#1a7f4b", "L1": "#2f6fb5", "L2": "#888", "L3": "#b5761a"}.get(tier, "#888")
        reason = SLOT_REASON_LABEL.get(item.get("_slot_reason") or "")
        conf = esc(item.get("confidence") or "")

        block = [
            '<div style="margin:14px 0 0">',
            f'<b style="font-size:15px;color:#111">{mark} {title}</b><br>',
            f'<span style="color:{tier_color};font-size:12px;font-weight:bold">{tier}</span>'
            + (f'<span style="color:{tier_color};font-size:12px"> {conf}</span>' if conf else "")
            + f'<span style="color:#999;font-size:12px"> · {score}分 · {esc(event_label(item.get("event_type")))}</span>',
        ]
        if reason:
            block.append(
                f'<span style="color:#b5761a;font-size:12px;font-weight:bold"> · {esc(reason)}</span>'
            )
        age = _age_note(item, date)
        if age:
            block.append(f'<span style="color:#999;font-size:12px">{esc(age)}</span>')
        if why:
            block.append(f'<div style="color:#555;font-size:13px;margin-top:4px">💡 {why}</div>')
        if summary:
            block.append(f'<div style="color:#333;font-size:14px;margin-top:4px">{summary}</div>')
        block.append(link(url, "原文链接 →"))
        block.append("</div>")
        parts.append("".join(block))

    # 槽 5
    parts.append('<hr style="border:none;border-top:1px solid #e5e5e5;margin:16px 0 10px">')
    if product_rows:
        as_of = max((r.get("data_date") or "" for r in product_rows), default="")
        moved = any(r.get("change_pct") is not None for r in product_rows)
        head = "⑤ 模型价格速览"
        if as_of:
            head += f'<span style="color:#999;font-size:12px">（数据日 {esc(as_of)}'
            head += " · 暂无变动）</span>" if not moved else "）</span>"
        parts.append(f'<b style="font-size:14px">{head}</b>')
        for row in product_rows[:3]:
            # 中文名优先：1745 个商品现在都有译名，读者不该再看到全英文货名
            name = esc(_clip(row.get("name_zh") or row.get("name") or "", 26))
            official = esc(row.get("official") or "")
            cn_official = esc(row.get("cn_official") or "")
            expected = esc(row.get("expected") or "")
            observed = esc(row.get("observed") or "")
            change = esc(row.get("change") or "")
            line = f'<div style="font-size:13px;color:#333;margin-top:4px">{name}'
            if official:
                line += f' <span style="color:#999">海外 {official}</span>'
            if cn_official:
                line += f' <span style="color:#c0392b">国内官店 {cn_official}</span>'
            elif expected:
                line += f' <span style="color:#c0392b">预期 {expected}</span>'
            if observed:
                line += f' <span style="color:#2f6fb5">最低 {observed}</span>'
            if change:
                line += f' <span style="color:#1a7f4b">{change}</span>'
            line += "</div>"
            parts.append(line)
        stale = max((int(r.get("stale_days") or 0) for r in product_rows), default=0)
        if stale > 7:
            parts.append(
                f'<div style="color:#b5761a;font-size:12px;margin-top:4px">'
                f"⚠ 价格数据已 {stale} 天未更新</div>"
            )
    elif product:
        parts.append(f'<b style="font-size:14px">⑤ 模型价格速览（{len(product)} 项）</b>')
        for p in product[:3]:
            parts.append(
                f'<div style="font-size:13px;color:#333;margin-top:4px">'
                f'{esc(_clip(p.get("title_zh") or p.get("canonical_title") or "", 30))}</div>'
            )
    elif slots.get("product_refs"):
        marks = "、".join(
            CIRCLED[i - 1] if 0 < i <= len(CIRCLED) else str(i) for i in slots["product_refs"]
        )
        parts.append(f'<span style="font-size:13px;color:#666">⑤ 模型价格：见 {marks}</span>')
    else:
        parts.append('<span style="font-size:13px;color:#666">⑤ 本周模型市场无重大变动</span>')

    # 槽 6
    if cn:
        age = float(cn.get("age_days") or 0)
        age_tag = f'<span style="color:#b5761a;font-size:12px"> · {age:.0f} 天前</span>' if age >= 1 else ""
        parts.append('<hr style="border:none;border-top:1px solid #e5e5e5;margin:14px 0 10px">')
        parts.append(
            f'<b style="font-size:14px">⑥ 【中文社区】</b>'
            f'<b style="font-size:14px;color:#111">{esc(_clip(cn.get("title_zh") or cn.get("canonical_title") or "", 34))}</b><br>'
            f'<span style="color:#999;font-size:12px">{cn.get("cn_score") or cn.get("importance_total", 0)}分</span>'
            f'{age_tag}'
        )
        if cn.get("why_matters"):
            parts.append(
                f'<div style="color:#555;font-size:13px;margin-top:3px">'
                f'💡 {esc(_clip(cn["why_matters"], 60))}</div>'
            )
        if cn.get("summary"):
            parts.append(
                f'<div style="color:#333;font-size:13px;margin-top:3px">'
                f'{esc(_clip(cn["summary"], 300))}</div>'
            )
        parts.append(link(cn.get("primary_url") or "", "原文链接 →", 12))

    parts.append('<hr style="border:none;border-top:1px solid #e5e5e5;margin:16px 0 8px">')
    parts.append(link(report_url, "查看完整版（含评分明细）→", 12))

    html = "".join(parts)
    data = html.encode("utf-8")
    if len(data) > max_bytes:
        # 按块（</div>）边界回退，避免切在标签或中文中间造成残标签
        cut = data[: max(0, max_bytes - 80)]
        idx = cut.rfind(b"</div>")
        if idx > 0:
            html = cut[: idx + len(b"</div>")].decode("utf-8", "ignore")
        else:
            html = cut.decode("utf-8", "ignore")
        html += (
            '<div style="color:#999;font-size:12px;margin-top:8px">…（内容超长已截断）</div>'
        )
    return html


def build_push_title(slots: dict[str, Any], date: str) -> str:
    """推送消息的标题（纯文本）。

    必须由排版层显式给出：HTML 版正文没有换行，Notifier 从"首行"取标题
    只会拿到一段标签源码（核查报告 BUG-02）。
    """
    base = f"【战锤日报 {date[5:]}】"
    tags: list[str] = []
    mode = MODE_LABELS.get(slots.get("mode", ""), "")
    if mode and mode != "正常":
        tags.append(mode)
    count = len(slots.get("general") or [])
    if 0 < count < 4:
        tags.append(f"仅{count}条")
    return base + (" " + " · ".join(tags) if tags else "")


def build_push_message(
    slots: dict[str, Any],
    date: str,
    report_url: str,
    content_type: str = "text",
    max_bytes: int = 2048,
    allow_split: bool = False,
) -> str:
    """按通道能力选择纯文本或 HTML 排版。

    allow_split=True 表示通道自己会分段发送（企业微信），此时不做硬截断，
    否则 Notifier 里的分段逻辑永远不会被触发（核查报告 BUG-29）。
    """
    if content_type == "html":
        return build_push_html(slots, date, report_url, max_bytes)
    return build_push_text(slots, date, report_url, max_bytes, allow_split=allow_split)


def build_report(
    slots: dict[str, Any],
    date: str,
    out_dir: Path,
    run_stats: dict[str, Any],
    llm_stats: dict[str, Any],
    failed_sources: list[str],
    source_names: dict[str, str] | None = None,
    effective_threshold: float = 60.0,
    llm_enabled: bool = True,
    write_latest: bool = True,
    weights: dict[str, float] | None = None,
    cn_weights: dict[str, float] | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tpl = _env.get_template("report.html.j2")
    names = source_names or {}
    # 防御：这个参数是「失败源 id 列表」。曾经上游把它覆盖成整数，
    # 结果整轮在推送前崩掉（11-21 12:00）。渲染不该因为一个展示字段炸掉整条推送。
    if not isinstance(failed_sources, (list, tuple, set)):
        log.warning("failed_sources 类型异常（%s），按空处理",
                    type(failed_sources).__name__)
        failed_sources = []

    def enrich(item: dict[str, Any]) -> dict[str, Any]:
        item = dict(item)
        raw = item.get("score_breakdown")
        if isinstance(raw, str):
            try:
                item["breakdown"] = json.loads(raw)
            except json.JSONDecodeError:
                item["breakdown"] = {}
        elif isinstance(raw, dict):
            item["breakdown"] = raw
        else:
            item["breakdown"] = {}

        # 展示分：二级补位用的是衰减后的分数，报告页必须和推送显示同一个数（N-03）
        item["shown_score"] = _shown_score(item)
        item["decay_factor"] = (
            round(item["shown_score"] / item["importance_total"], 3)
            if item.get("importance_total") else 1.0
        )

        ids = item.get("source_ids") or []
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except json.JSONDecodeError:
                ids = [ids]
        item["source_labels"] = [names.get(s, s) for s in ids]

        # 中文社区卡的明细也是 JSON 字符串，模板要按 dict 用
        raw_cn = item.get("cn_breakdown")
        if isinstance(raw_cn, str):
            try:
                item["cn_breakdown"] = json.loads(raw_cn)
            except json.JSONDecodeError:
                item["cn_breakdown"] = {}
        elif not isinstance(raw_cn, dict):
            item["cn_breakdown"] = {}

        # 规则模式下 quote 就是标题，重复显示没有意义
        quote = (item.get("quote_en") or "").strip()
        title = (item.get("canonical_title") or "").strip()
        if quote and quote[:60] == title[:60]:
            item["quote_en"] = ""
        return item

    html = tpl.render(
        date=date,
        mode=slots.get("mode", "normal"),
        mode_label=MODE_LABELS.get(slots.get("mode", "normal"), slots.get("mode", "")),
        threshold=round(effective_threshold, 0),
        rules_mode=not llm_enabled,
        general=[enrich(x) for x in slots.get("general") or []],
        product=[enrich(x) for x in slots.get("product") or []],
        product_rows=slots.get("product_rows") or [],
        product_refs=slots.get("product_refs") or [],
        price_coverage=slots.get("price_coverage") or {},
        cn=enrich(slots["cn"]) if slots.get("cn") else None,
        stats=slots.get("stats", {}),
        run_stats=run_stats,
        llm_stats=llm_stats,
        failed_sources=[names.get(f, f) for f in failed_sources],
        generated_at=datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        event_labels=EVENT_LABELS,
        slot_reason_labels=SLOT_REASON_LABEL,
        dim_labels=MODEL_DIM_LABELS,
        cn_dim_labels=CN_DIM_LABELS,
        cn_weights=cn_weights or {},
        weights=weights or {},
        feedback_token=feedback_token(date),
    )
    path = out_dir / f"{date}.html"
    path.write_text(html, encoding="utf-8")

    if write_latest:
        latest = out_dir / "latest.html"
        latest.write_text(html, encoding="utf-8")
    return path


