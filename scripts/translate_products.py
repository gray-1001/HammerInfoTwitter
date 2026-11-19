# -*- coding: utf-8 -*-
"""批量把商品英文名翻成中文，写入 products.name_zh 并留痕。

用法：
    python scripts/translate_products.py --limit 40            # 试跑 40 条
    python scripts/translate_products.py                        # 全部缺中文名的
    python scripts/translate_products.py --force --limit 100    # 重译
    python scripts/translate_products.py --dry-run --limit 20   # 只看不写

设计要点：
- **批量**：一次 20 条，避免 1745 次单条调用；
- **术语表**：只把本批出现的术语塞进提示词，保证同词同译；
- **校验**：条数必须对齐、必须含中文、长度合理、不能是英文原文；
- **留痕**：products.name_zh_source / name_zh_at + name_translations 历史表。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from infotwitter.config import load_config  # noqa: E402
from infotwitter.db import CST, Database, now_iso  # noqa: E402
from infotwitter.llm import LLMClient  # noqa: E402
from infotwitter.pricing.glossary import relevant_glossary  # noqa: E402

GLOSSARY_VER = "2026-11-18a"
CJK = re.compile(r"[\u4e00-\u9fff]")

# 产品线前缀：统一成「前缀：正文」，否则同一批里会出现「帝国军团 犀牛」和「帝国军团：犀牛」
LINE_PREFIXES = (
    "战锤40K", "战锤40k", "帝国军团", "西格玛时代", "荷鲁斯之乱", "旧世界",
    "涅克洛蒙达", "血碗", "杀戮小队", "泰坦修会", "中土", "哥特舰队",
    "底色漆", "层涂漆", "对比漆", "洗涂漆", "干扫漆", "技术漆", "气泵漆", "基础漆", "荒区",
)


def normalize_zh(zh: str) -> str:
    """统一分隔符与空白，去掉重复的产品线前缀。"""
    s = re.sub(r"\s+", " ", (zh or "").strip())
    for p in LINE_PREFIXES:
        if s.startswith(p + " "):
            s = p + "：" + s[len(p):].strip()
        # 「帝国军团：帝国军团 xxx」这种重复
        if s.startswith(p + "：" + p):
            s = p + s[len(p) * 2 + 1:]
    return s.strip()

SYSTEM = """你是战锤模型领域的商品命名专家。你的任务是把 GW（Games Workshop）官方英文商品名
翻译成中国玩家在淘宝/闲鱼/贴吧实际会用的中文名称。

硬性要求：
1. 只输出 JSON，形如 {"items":[{"id":"货号","zh":"中文名"}]}，条数与输入完全一致；
2. 每条中文名 4–30 字，必须包含中文汉字；
3. **必须使用给定术语表中的译名**，不得自创说法；
4. 包装/版本标记按术语表处理，如 (CHN)→（中文版）、(HB)→（精装）；
5. 保留容量与数量：12ML / 18ML / 24ML 原样保留，ROW X6 / 6 PACK 译成「6支装」；
6. 不要输出引号、不要解释、不要拼音。"""

USER_TMPL = """术语表（必须遵守，格式 英文=中文）：
{glossary}

待翻译商品（货号 | 英文名）：
{items}

请输出 JSON：{{"items":[{{"id":"货号","zh":"中文名"}}]}}"""


def build_prompt(batch: list[dict]) -> str:
    names = [b["name_en"] for b in batch]
    terms = relevant_glossary(names, max_terms=50)
    glossary = "\n".join(terms) if terms else "（本批没有命中术语表，按社区通用叫法翻译）"
    items = "\n".join(f"{b['id']} | {b['name_en']}" for b in batch)
    return USER_TMPL.format(glossary=glossary, items=items), glossary


def parse_reply(text: str, batch: list[dict]) -> dict[str, str]:
    """解析并校验，返回 {id: zh}；不合格的条目直接丢弃（留给下次重试）。"""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 模型偶尔会在 JSON 外面包一层说明，兜底抓最外层花括号
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}
    by_id = {b["id"]: b["name_en"] for b in batch}
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "").strip()
        zh = str(it.get("zh") or "").strip().strip('"').strip()
        zh = normalize_zh(zh)
        if pid not in by_id:
            continue
        if not zh or not CJK.search(zh):
            continue
        if len(zh) > 40 or len(zh) < 2:
            continue
        if zh.lower() == by_id[pid].lower():
            continue
        out[pid] = zh
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true", help="重译已有中文名的")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-category", default="", help="只处理某个品类，如 paint")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.db_path)
    sql = "SELECT id, name_en, category FROM products WHERE 1=1"
    params: list = []
    if not args.force:
        sql += " AND (name_zh IS NULL OR name_zh='')"
    if args.only_category:
        sql += " AND category=?"
        params.append(args.only_category)
    sql += " ORDER BY id"
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    rows = [dict(r) for r in db.conn.execute(sql, params)]
    if not rows:
        print("没有需要翻译的商品。")
        db.close()
        return 0

    client = LLMClient(cfg)
    if not client.enabled:
        print("LLM 不可用（检查配置）")
        db.close()
        return 1
    print(f"待翻译 {len(rows)} 条，批次 {args.batch}，并发 {args.workers}")

    batches = [rows[i:i + args.batch] for i in range(0, len(rows), args.batch)]
    results: dict[str, str] = {}
    failed_batches = 0

    def run(batch: list[dict]):
        prompt, _ = build_prompt(batch)
        content = client._chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            json_mode=True,
        )
        return batch, content

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for batch, content in pool.map(run, batches):
            got = parse_reply(content or "", batch)
            if not got:
                failed_batches += 1
            results.update(got)
            done += len(batch)
            print(f"  进度 {done}/{len(rows)}，本批 {len(got)}/{len(batch)}")

    print(f"\n翻译成功 {len(results)}/{len(rows)} 条，失败批次 {failed_batches}")

    if args.dry_run:
        for pid, zh in list(results.items())[:20]:
            name = next(r["name_en"] for r in rows if r["id"] == pid)
            print(f"  {pid:<8} {name[:44]:<46} → {zh}")
        db.close()
        return 0

    ts = now_iso()
    source = f"{client.provider}/{client.model}"
    with db.tx() as conn:
        for pid, zh in results.items():
            conn.execute(
                "UPDATE products SET name_zh=?, name_zh_source=?, name_zh_at=? WHERE id=?",
                (zh, source, ts, pid),
            )
            conn.execute(
                "INSERT OR REPLACE INTO name_translations"
                " (product_id, name_en, name_zh, source, glossary_ver, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (pid, next(r["name_en"] for r in rows if r["id"] == pid), zh,
                 source, GLOSSARY_VER, ts),
            )
    print(f"已写入 {len(results)} 条（source={source}）")
    left = db.conn.execute(
        "SELECT COUNT(*) c FROM products WHERE name_zh IS NULL OR name_zh=''"
    ).fetchone()["c"]
    print(f"仍缺中文名 {left} 条")
    db.close()
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
