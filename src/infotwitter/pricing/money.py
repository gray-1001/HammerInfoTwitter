"""汇率与人民币换算。"""
from __future__ import annotations

from typing import Any
from .common import DEFAULT_RATES


def get_rates(cfg: Any) -> dict[str, float]:
    rates = dict(DEFAULT_RATES)
    configured = (cfg.get("pricing.rates", {}) or {}) if cfg else {}
    for key, value in configured.items():
        currency = str(key).replace("_CNY", "").upper()
        try:
            rates[currency] = float(value)
        except (TypeError, ValueError):
            continue
    return rates


def to_cny(amount: float, currency: str, rates: dict[str, float]) -> float | None:
    rate = rates.get(currency.upper())
    return round(amount * rate, 2) if rate else None
