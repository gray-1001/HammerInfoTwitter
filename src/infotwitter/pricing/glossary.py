# -*- coding: utf-8 -*-
"""战锤商品名术语表：英文 → 中文（社区通用叫法）。

用途：给 LLM 批量翻译时提供一致的锚点，避免同一个词在不同批次翻成不同说法
（"CUSTODIAN GUARD" 一会儿「守卫长矛兵」一会儿「禁军卫士」）。
只收高频、易分歧的词；长尾交给模型自行判断。

约定：
- 派系名用中国玩家实际叫法（禁军 / 战斗修女 / 灵族 / 兽人 …）
- 涂料系列用官方中文名（底色 / 层涂 / 对比漆 / 洗涂 / 干扫 / 技术漆 / 气泵）
- 产品形态用直译（圣典 / 战典 / 规则书 / 新手包 / 战斗巡逻 / 先锋军）
"""
from __future__ import annotations

GLOSSARY: dict[str, str] = {
    # ---- 产品线 ----
    "WARHAMMER 40,000": "战锤40K",
    "WARHAMMER 40000": "战锤40K",
    "WH40K": "战锤40K",
    "WARHAMMER 40K": "战锤40K",
    "AGE OF SIGMAR": "西格玛时代",
    "HORUS HERESY": "荷鲁斯之乱",
    "THE HORUS HERESY": "荷鲁斯之乱",
    "THH": "荷鲁斯之乱",
    "THE OLD WORLD": "旧世界",
    "TOW": "旧世界",
    "LEGIONS IMPERIALIS": "帝国军团",
    "LEGION IMPERIALIS": "帝国军团",
    "L/I": "帝国军团",
    "ADEPTUS TITANICUS": "泰坦修会",
    "BATTLEFLEET GOTHIC": "哥特舰队",
    "KILL TEAM": "杀戮小队",
    "NECROMUNDA": "涅克洛蒙达",
    "BLOOD BOWL": "血碗",
    "MIDDLE-EARTH": "中土",
    "M-E SBG": "中土战略战斗",
    "SBG": "战略战斗",
    # ---- 星际战士系 ----
    "SPACE MARINES": "星际战士",
    "SM": "星际战士",
    "LEGIONES ASTARTES": "军团星际战士",
    "CHAOS SPACE MARINES": "混沌星际战士",
    "CSM": "混沌星际战士",
    "DEATHWATCH": "死亡守望",
    "BLOOD ANGELS": "血天使",
    "DARK ANGELS": "暗黑天使",
    "SPACE WOLVES": "太空野狼",
    "ULTRAMARINES": "极限战士",
    "IMPERIAL FISTS": "帝国之拳",
    "SALAMANDERS": "火蜥蜴",
    "RAVEN GUARD": "暗鸦守卫",
    "IRON HANDS": "钢铁之手",
    "WHITE SCARS": "白色伤疤",
    "BLACK TEMPLARS": "黑色圣堂",
    "GREY KNIGHTS": "灰骑士",
    "DEATH GUARD": "死亡守卫",
    "THOUSAND SONS": "千子",
    "WORLD EATERS": "吞世者",
    "EMPEROR'S CHILDREN": "帝皇之子",
    "BLACK LEGION": "黑色军团",
    # ---- 帝国其他 ----
    "ADEPTUS CUSTODES": "禁军",
    "CUSTODES": "禁军",
    "ADEPTA SORORITAS": "战斗修女",
    "ASTRA MILITARUM": "星界军",
    "A/MILITARUM": "星界军",
    "IMPERIAL GUARD": "帝国卫队",
    "ADEPTUS MECHANICUS": "机械修会",
    "IMPERIAL KNIGHTS": "帝国骑士",
    "CHAOS KNIGHTS": "混沌骑士",
    "AGENTS OF THE IMPERIUM": "帝国特工",
    "GREY KNIGHTS": "灰骑士",
    # ---- 异形 ----
    "AELDARI": "灵族",
    "CRAFTWORLDS": "方舟灵族",
    "DRUKHARI": "德鲁凯瑞",
    "HARLEQUINS": "丑角",
    "YNNARI": "伊纳里",
    "ORKS": "兽人",
    "NECRONS": "太空死灵",
    "TYRANIDS": "泰伦虫族",
    "GENESTEALER CULTS": "基因窃取者教派",
    "T'AU EMPIRE": "钛帝国",
    "TAU EMPIRE": "钛帝国",
    "LEAGUES OF VOTANN": "沃坦联盟",
    # ---- 西格玛时代 ----
    "STORMCAST ETERNALS": "雷铸神军",
    "SKAVEN": "斯卡文鼠人",
    "NIGHTHAUNT": "夜幽",
    "GLOOMSPITE GITZ": "幽光哥布林",
    "ORRUK WARCLANS": "兽人战争氏族",
    "MAGGOTKIN OF NURGLE": "纳垢蛆虫族",
    "DAUGHTERS OF KHAINE": "凯恩之女",
    "IDONETH DEEPKIN": "深海精灵",
    "SYLVANETH": "森林精灵",
    "LUMINETH REALM-LORDS": "光域领主",
    "FYRESLAYERS": "火矮人",
    "KHARADRON OVERLORDS": "卡拉德隆霸主",
    "SERAPHON": "蜥蜴人",
    "SOULBLIGHT GRAVELORDS": "魂疫墓领主",
    "FLESH-EATER COURTS": "食肉王庭",
    "HEDONITES OF SLAANESH": "色孽享乐者",
    "DISCIPLES OF TZEENTCH": "奸奇信徒",
    "BLADES OF KHORNE": "恐虐之刃",
    "OSSIARCH BONEREAPERS": "骨王",
    "CITIES OF SIGMAR": "西格玛之城",
    "HIGH ELF REALMS": "高等精灵",
    "WOOD ELF REALMS": "木精灵",
    "DWARFEN HOLDS": "矮人堡",
    "TOMB KINGS": "墓王",
    "KISLEV": "基斯里夫",
    "ARCANE JOURNAL": "秘法志",
    # ---- 产品形态 ----
    "CORE RULES": "核心规则",
    "CORE BOOK": "核心规则书",
    "RULEBOOK": "规则书",
    "CODEX SUPPLEMENT": "圣典增补",
    "CODEX": "圣典",
    "BATTLETOME": "战典",
    "GENERAL'S HANDBOOK": "将军手册",
    "CHAPTER APPROVED": "战团批准",
    "MISSION DECK": "任务卡组",
    "MATCHED PLAY": "对抗赛",
    "STARTER SET": "新手包",
    "CORE SET": "核心套装",
    "BOXED SET": "盒装套装",
    "COMBAT PATROL": "战斗巡逻",
    "SPEARHEAD": "先锋军",
    "ARMAGEDDON BATTALION": "阿米吉多顿营",
    "BATTALION": "营",
    "BOARDING ACTIONS": "登舰战",
    "TERRAIN AREA SET": "地形套装",
    "DICE SET": "骰子套装",
    "CARDS": "卡牌",
    "TOKENS": "标记",
    "MARKERS": "标记物",
    "TERRAIN": "地形",
    # 涅克洛蒙达的 Badzone 地形系列
    "B/Z": "荒区",
    "BADZONE": "荒区",
    # 涂料前缀（单字母/短码）
    "B:": "底色漆",
    "L:": "层涂漆",
    "BASE:": "底色漆",
    "INCUBI": "因库比",
    "FRONTERIS": "弗龙特里斯",
    "HAB-BUNKER": "居住地堡",
    "STOCKADE": "围栏",
    "ARMOURED CONTAINERS": "装甲集装箱",
    "PAINTING HANDLE": "涂装手柄",
    "WATER POT": "洗笔罐",
    "BRUSH": "画笔",
    # ---- 涂料系列（前缀） ----
    "CONTRAST": "对比漆",
    "SHADE": "洗涂漆",
    "TECHNICAL": "技术漆",
    "DRY": "干扫漆",
    "AIR": "气泵漆",
    "LAYER": "层涂漆",
    "BASE": "底色漆",
    "SPRAY": "喷漆",
    "ROW X6": "整条6支装",
    "X6": "6支装",
    "6 PACK": "6支装",
    "(6-PACK)": "6支装",
    "(X6)": "6支装",
    "PACK": "套装",
    # ---- 常见通用词 ----
    "GUARD": "守卫",
    "WARDENS": "守望者",
    "CAPTAIN": "队长",
    "LORD": "领主",
    "CHAMPION": "勇士",
    "SQUAD": "小队",
    "SQUADRON": "中队",
    "TEAM": "小队",
    "WARRIORS": "战士",
    "KNIGHTS": "骑士",
    "TANK": "坦克",
    "DREADNOUGHT": "无畏机甲",
    "TERMINATOR": "终结者",
    "INTERCESSORS": "突击仲裁者",
    "BATTLE SISTERS": "战斗修女",
    "SISTERS OF SILENCE": "寂静修女",
    "CUSTODIAN GUARD": "守卫长矛兵",
    "CUSTODIAN WARDENS": "守望者",
    "ALLARUS CUSTODIANS": "阿拉鲁斯禁军",
    "VERTUS PRAETORS": "维图斯执政官",
    "TRAJANN VALORIS": "图拉真·瓦洛里斯",
    "BLADE CHAMPION": "刃之勇士",
    "SHIELD CAPTAIN": "盾卫队长",
}

# 版本/语言标记：翻译时保留为中文标注
LANG_MARKS = {
    "(CHN)": "（中文版）",
    "(CHINESE)": "（中文版）",
    "(ENG)": "（英文版）",
    "(ENGLISH)": "（英文版）",
    "(HB)": "（精装）",
}


def glossary_lines(limit: int = 0, only: list[str] | None = None) -> list[str]:
    """把术语表排成 "EN=CN" 行，供提示词使用。limit=0 表示全部。"""
    items = sorted(GLOSSARY.items(), key=lambda kv: -len(kv[0]))
    if only:
        wanted = {o.upper() for o in only}
        items = [kv for kv in items if kv[0].upper() in wanted]
    if limit:
        items = items[:limit]
    return [f"{en}={zh}" for en, zh in items]


def relevant_glossary(names: list[str], max_terms: int = 60) -> list[str]:
    """只挑这批名字里出现的术语，避免提示词过长。"""
    blob = " ".join(n.upper() for n in names)
    hits = [en for en in GLOSSARY if en.upper() in blob]
    hits.sort(key=lambda en: (-len(en), en))
    return [f"{en}={GLOSSARY[en]}" for en in hits[:max_terms]]
