"""价格模块：商品主表、多渠道价格采集、价格摘要表。

数据流：
    GW 官方调价表(Excel)  -> products + cn_official / cn_trade 价格点
    Element Games(英国零售) -> gw_uk 价格点
    淘宝 / 闲鱼(需登录)     -> taobao / xianyu 价格点
                          ↓
                   价格摘要表（海外价 / 官店价 / 国内价 / 最低价及平台）

设计原则：
- 价格永远是「观测点」而不是「字段」，同一商品同一渠道会有多条带时间戳的记录，
  这样才能看出涨跌，也能在渠道失效时保留历史。
- 主表只存商品身份（货号、条码、名称），不存价格。
"""
from __future__ import annotations

import json
import logging
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import CST, now_iso

log = logging.getLogger(__name__)

# 用于人民币换算的兜底汇率（配置里可覆盖）
DEFAULT_RATES = {"GBP": 9.20, "USD": 7.20, "EUR": 7.80, "CNY": 1.0}

# 渠道展示名
CHANNEL_LABEL = {
    "gw_uk": "海外零售",
    "gw_us": "海外零售",
    "gw_official": "GW 官网",
    "cn_official": "国内官店",
    "cn_trade": "国内渠道",
    "taobao": "淘宝",
    "xianyu": "闲鱼",
    "manual": "手工录入",
}

# 只能由系统采集写入的渠道。人工 CSV 不允许碰它们，否则一条
# `channel=cn_official, price=1.5` 就能把"国内官店价"改成 1.5 元（P-01）。
OFFICIAL_CHANNELS = frozenset({"cn_official", "cn_trade", "gw_uk", "gw_us", "gw_official"})
# 人工 CSV 允许写入的渠道
MANUAL_CHANNELS = frozenset({"manual", "taobao", "xianyu", "cn_retail"})
# 系统平台的展示名，人工数据不得占用（唯一键含 platform，会覆盖官方观测）
RESERVED_PLATFORMS = frozenset({"gw china rrp", "gw china trade", "element games", "gw official"})

# 置信度：low 的观测一律不参与展示、不算涨跌（P-02）
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _confidence(value: Any) -> str:
    text = str(value or "medium").strip().lower()
    return text if text in CONFIDENCE_RANK else "medium"


def _usable(point: dict[str, Any] | None) -> bool:
    """能不能拿去给读者看。只挡 low：它是"多半匹配错了"的信号。"""
    if not point:
        return False
    return CONFIDENCE_RANK[_confidence(point.get("confidence"))] >= CONFIDENCE_RANK["medium"]


def _weakest(points: list[dict[str, Any] | None]) -> str:
    """一组展示值的整体置信度取最弱的一个——不能拿官店价的 high 去背书闲鱼价。"""
    ranks = [_confidence(p.get("confidence")) for p in points if p]
    if not ranks:
        return ""
    return min(ranks, key=lambda c: CONFIDENCE_RANK[c])


def normalize_observed_at(value: Any) -> str:
    """把观测时间统一成 `YYYY-MM-DD`。

    `observed_at` 是唯一键的一部分，格式一乱同一天的观测就会重复入库，
    直接制造假涨跌（P-05）。解析不了就返回空串，调用方必须当"非法"处理。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    head = text[:10].replace("/", "-")
    try:
        return datetime.strptime(head, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def _valid_price(price: Any, *, low: float = 0.0, high: float = 100000.0) -> bool:
    """价格必须是正的有限数：负价会直接被 min() 选成"最低价"展示出去（P-14）。"""
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        return False
    value = float(price)
    return value == value and low < value <= high  # NaN 不等于自己
