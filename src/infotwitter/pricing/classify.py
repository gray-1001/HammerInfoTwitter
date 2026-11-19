"""商品分类：产品线（40k/aos/30k/tow）与品类（codex/boxed/mini…）。"""
from __future__ import annotations

import re
from typing import Any


def classify_line(name: str) -> str:
    low = (name or "").lower()
    if any(k in low for k in ("horus heresy", "30k", "legiones", "solar auxilia", "legion imperialis")):
        return "30k"
    if any(k in low for k in ("age of sigmar", "sigmar", "stormcast", "skaven", "soulblight",
                              "kharadron", "seraphon", "fyreslayer", "idoneth", "nighthaunt",
                              "gloomspite", "ironjawz", "kruleboyz", "lumineth", "daughters of khaine",
                              "slaves to darkness", "hedonites", "disciples of tzeentch",
                              "magotkin", "blades of khorne", "ossiarch", "cities of sigmar")):
        return "aos"
    if any(k in low for k in ("old world", "bretonnia", "tomb kings", "khemri", "cathay",
                              "empire of man", "dwarfen mountain", "warriors of chaos")):
        return "tow"
    if any(k in low for k in (
        "40k", "40000", "40,000", "astartes", "space marine", "imperium", "chaos",
        "necron", "tyranid", "aeldari", "eldar", "ork", "tau", "adeptus", "astra militarum",
        "sororitas", "mechanicus", "custodes", "csm", "a/militarum", "a/custodes", "a/mechanicus",
        "admek", "deathwatch", "death guard", "thousand sons", "world eaters", "emperors children",
        "emperor's children", "blood angels", "dark angels", "space wolves", "grey knights",
        "black templars", "custodian", "armageddon", "battlezone", "kill team", "combat patrol",
        "cadian", "catachan", "krieg", "templars", "sisters of battle",
    )):
        return "40k"
    return "other"


def classify_category(name: str) -> str:
    low = (name or "").lower()
    if any(k in low for k in ("battlezone", "b/z", "terrain", "scenery", "hab-bunker")):
        return "terrain"
    if any(k in low for k in ("codex", "battletome", "army book", "chapter approved", "index")):
        return "codex"
    # 容量标记（12ML / 24ML / 6-PACK）基本都是涂料，名字里不一定带 "paint"
    if re.search(r"\b\d+\s*ml\b", low):
        return "paint"
    if any(k in low for k in ("paint", "colour", "layer", "shade", "technical", "contrast",
                              "spray", "brush", "glue", "tool", "basius")):
        return "paint"
    if any(k in low for k in ("novel", "paperback", "hardback", "black library", "anthology",
                              "audio", "ebook", "book")):
        return "book"
    if any(k in low for k in ("combat patrol", "battleforce", "starter", "battalion", "boxed",
                              "battle box", "launch set", "core set", "army set", "value")):
        return "boxed"
    if any(k in low for k in ("dice", "card", "template", "token", "board", "mat")):
        return "accessory"
    return "mini"


def _to_float(value: Any) -> float | None:
    """兼容 '1330'、'1330.00'、'¥1,330'、'1,330.5' 等写法（BUG-33）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace(",", "").replace("¥", "").replace("￥", "").replace("£", "").strip()
    try:
        return float(text)
    except ValueError:
        return None
