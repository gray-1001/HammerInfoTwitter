"""价格模块：导入、采集、摘要与展示。

对外接口保持不变：外部一律 `from .pricing import X`，
内部按职责拆分为 common / classify / importers / money / summary / overseas / cn。
"""
from __future__ import annotations

from .common import CHANNEL_LABEL, CONFIDENCE_RANK, DEFAULT_RATES, MANUAL_CHANNELS, OFFICIAL_CHANNELS, RESERVED_PLATFORMS, log, normalize_observed_at
from .classify import classify_category, classify_line
from .importers import auto_refresh, import_gw_excel, import_manual_csv, latest_price_date, purge_mismatched_overseas, record_price_run
from .money import get_rates, to_cny
from .summary import build_slot5_rows, build_summary, expected_price, latest_points, price_coverage, price_movements, render_summary
from .overseas import EDITION_TOKENS, clean_query, edition_signature, fetch_element_games, query_variants, save_overseas_points
from .cn import DIGITAL_MARKERS, NAME_STOPWORDS, TRANSLATE_PROMPT, core_tokens, is_digital_goods, search_queries, select_relevant, translate_names

# 测试直接引用的内部助手：旧单文件结构下它们本来就可导入，
# 拆包后继续从包顶层导出，避免测试被迫感知内部布局。
from .common import _usable, _valid_price, _weakest, _confidence  # noqa: F401
from .overseas import _category_family, _similarity, _norm, _tokens  # noqa: F401
from .summary import _safe_url  # noqa: F401

__all__ = [
    "_usable",
    "_valid_price",
    "_weakest",
    "_confidence",
    "_category_family",
    "_similarity",
    "_norm",
    "_tokens",
    "_safe_url",
    # common
    "CHANNEL_LABEL",
    "CONFIDENCE_RANK",
    "DEFAULT_RATES",
    "MANUAL_CHANNELS",
    "OFFICIAL_CHANNELS",
    "RESERVED_PLATFORMS",
    "log",
    "normalize_observed_at",
    # classify
    "classify_category",
    "classify_line",
    # importers
    "auto_refresh",
    "import_gw_excel",
    "import_manual_csv",
    "latest_price_date",
    "purge_mismatched_overseas",
    "record_price_run",
    # money
    "get_rates",
    "to_cny",
    # summary
    "build_slot5_rows",
    "build_summary",
    "expected_price",
    "latest_points",
    "price_coverage",
    "price_movements",
    "render_summary",
    # overseas
    "EDITION_TOKENS",
    "clean_query",
    "edition_signature",
    "fetch_element_games",
    "query_variants",
    "save_overseas_points",
    # cn
    "DIGITAL_MARKERS",
    "NAME_STOPWORDS",
    "TRANSLATE_PROMPT",
    "core_tokens",
    "is_digital_goods",
    "search_queries",
    "select_relevant",
    "translate_names",
]
