"""国内源：中文品名生成、数字商品过滤、结果相关性筛选。"""
from __future__ import annotations

import logging
import re
from typing import Any
from .common import log


TRANSLATE_PROMPT = """你是战锤模型领域的商品命名专家。把下面的英文商品名翻译成中国玩家在淘宝/闲鱼搜索时实际会用的中文名称。

要求：
1. 只输出中文商品名，不要解释、不要引号
2. 保留玩家通用的英文缩写（如 40K、HH、SM），其他尽量用中文常用称呼
3. 控制在 20 字以内，去掉包装/版本等冗余词
4. 例：ARMAGEDDON BATTLEON: DEATHWATCH -> 战锤40K 死亡守望 阿米吉多顿大战役包

英文名：{name}
中文名："""

# 数字商品/账号类，与实体模型无关，国内搜索结果里要排除
# 这些不是「实体模型」，混进来会把中位价拉到几块钱，必须挡掉。
DIGITAL_MARKERS = (
    # 游戏/账号类
    "激活码", "cdkey", "cdk", "steam", "兑换码", "游戏key", "账号", "代充",
    "会员", "点卡", "充值", "下载码", "离线", "共享", "租号",
    # 3D 打印图纸/数模/文件
    "图纸", "stl", "3d打印", "3d 打印", "数模", "模型文件", "打印文件",
    "模型包", "素材包", "源文件", "obj", "step文件", "免费模型",
    # 代工/服务/扫描件
    "代工", "代打", "打印服务", "建模服务", "定制服务", "扫描件", "电子版",
    # 预定/代购/转运等非现货
    "代购", "转运", "预定金", "定金",
)


def is_digital_goods(title: str) -> bool:
    low = (title or "").lower()
    return any(m in low for m in DIGITAL_MARKERS)


def translate_names(conn: Any, cfg: Any, limit: int = 30, only_missing: bool = True) -> int:
    """用 LLM 为商品生成中文名，写入 products.name_zh。"""
    from ..llm import LLMClient

    sql = "SELECT id, name_en FROM products WHERE 1=1"
    if only_missing:
        sql += " AND (name_zh IS NULL OR name_zh='')"
    sql += " AND category NOT IN ('paint','terrain','accessory','book') ORDER BY id LIMIT ?"
    rows = conn.execute(sql, (limit,)).fetchall()
    if not rows:
        return 0

    client = LLMClient(cfg)
    if not client.enabled:
        log.warning("LLM 不可用，跳过中文名生成")
        return 0

    done = 0
    for r in rows:
        prompt = TRANSLATE_PROMPT.format(name=r["name_en"])
        content = client._chat([{"role": "user", "content": prompt}], json_mode=False)
        if not content:
            continue
        name_zh = content.strip().strip('"').strip()
        if name_zh.startswith("{") and '"' in name_zh:
            import json as _json

            try:
                data = _json.loads(name_zh)
                name_zh = next((str(v) for v in data.values() if isinstance(v, str)), "")
            except Exception:  # noqa: BLE001
                name_zh = ""
        name_zh = name_zh.replace("\n", " ")[:60]
        if not name_zh or len(name_zh) < 2:
            continue
        conn.execute("UPDATE products SET name_zh=? WHERE id=?", (name_zh, r["id"]))
        done += 1
    conn.commit()
    client.close()
    log.info("生成中文品名 %d/%d 条", done, len(rows))
    return done


# 中文品名里大量重复的泛词，不能用来判断相关性
NAME_STOPWORDS = {"战锤", "40k", "30k", "战锤40k", "模型", "模型套件", "套件", "全新", "现货"}


def _split_zh(name_zh: str) -> list[str]:
    """把中文品名切成词：括号内容丢掉，全角/半角标点一律当分隔符。

    译名统一成「禁军：阿拉鲁斯禁军」这种带全角冒号的格式后，如果只按空格切，
    整串会变成一个 token，`tk in title` 永远不成立，相关度筛选会把所有结果拒掉
    （实测 12/12 全被筛除）。
    """
    text = re.sub(r"[（(][^）)]*[）)]", " ", name_zh or "")
    text = re.sub(r"[：:，,、/／·•|｜]+", " ", text)
    return [p.strip() for p in text.split() if p.strip()]


def core_tokens(name_zh: str) -> list[str]:
    """从中文品名里取「能标识这件商品」的词。"""
    tokens = [p for p in _split_zh(name_zh) if len(p) >= 2 and p.lower() not in NAME_STOPWORDS]
    return tokens[:4]


def select_relevant(
    items: list[dict[str, Any]],
    name_zh: str,
    official_cny: float | None,
    low_ratio: float = 0.10,
    high_ratio: float = 2.5,
    min_tokens: int = 1,
) -> tuple[list[dict[str, Any]], str]:
    """过滤出与目标商品相关、且价格落在合理区间的条目。

    返回 (保留条目, 说明)。说明用于记录到 note 里，方便回溯为什么是这个价。
    """
    tokens = core_tokens(name_zh)
    kept: list[dict[str, Any]] = []
    dropped_token = dropped_price = 0

    for it in items:
        title = it.get("title") or ""
        if tokens and sum(1 for tk in tokens if tk in title) < min_tokens:
            dropped_token += 1
            continue
        if official_cny and official_cny > 0:
            ratio = it["price"] / official_cny
            if not (low_ratio <= ratio <= high_ratio):
                dropped_price += 1
                continue
        kept.append(it)

    reason = f"相关筛除 {dropped_token}，价格异常筛除 {dropped_price}"
    return kept, reason


def search_queries(name_zh: str, max_parts: int = 2) -> list[str]:
    """按「由准到宽」返回候选查询词。

    先用完整译名（最准，但常常搜不到），不够再退回「品牌 + 阵营」。
    只用宽词会让同阵营的不同单位（守卫长矛兵 / 寂静修女）拿到同一批货、
    同一个中位价——价格就失真了。

    标点必须清掉：译名统一成「禁军：阿拉鲁斯禁军」这种格式后，
    带着全角冒号丢给闲鱼会一条都搜不到（实测 3/3 返回 0 条）。
    """
    text = re.sub(r"[（(][^）)]*[）)]", " ", name_zh or "")
    # 全角/半角冒号、顿号、逗号、斜杠、中点都当分隔符
    text = re.sub(r"[：:，,、/／·•]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    parts = [p for p in text.split(" ") if p]
    out: list[str] = []
    if len(parts) > max_parts:
        out.append(" ".join(parts))              # 完整译名
        out.append(" ".join(parts[-max_parts:]))  # 去掉产品线前缀，留「阵营 + 型号」
        out.append(" ".join(parts[:max_parts]))   # 产品线 + 阵营
    else:
        out.append(text)
    seen: set[str] = set()
    return [q for q in out if q and not (q in seen or seen.add(q))]
