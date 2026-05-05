"""
csp.py
Bull Put Credit Spread scanner — OTM short puts for stock acquisition at discount.

Goal: collect premium for selling OTM puts on quality dividend stocks.
- If stock stays above short strike → keep credit (pure income)
- If assigned → own stock at discount to current spot

For each ticker × each expiration in DTE_MIN..DTE_MAX:
  - Find a put with abs(delta) in [DELTA_MIN, DELTA_MAX]  (OTM, ~0.20-0.30)
  - Use it as SHORT leg (strike below spot)
  - Use the strike WIDTH below as LONG leg (further OTM, downside protection)
  - Mid-adjusted fills (sell mid-15%, buy mid+15%)
  - net_credit = short_credit - long_cost
  - max_risk = width - net_credit
  - apy_if_not_assigned = (net_credit / max_risk) * 365/dte * 100
  - Filter: max_risk > 0, apy >= MIN_APY_PCT
"""

import logging
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

DTE_MIN              = 30
DTE_MAX              = 90
DELTA_MIN            = 0.20
DELTA_MAX            = 0.30
WIDTH                = 2.50
MID_ADJUST_FRAC      = 0.15
MIN_APY_PCT          = 12.0
MAX_PRICE            = 43.0


def _has_market(option):
    bid = option.get("bid") or 0.0
    ask = option.get("ask") or 0.0
    return bid > 0 and ask > 0


def _bid(option):
    return float(option.get("bid") or 0.0)


def _ask(option):
    return float(option.get("ask") or 0.0)


def _oi(option):
    return int(option.get("openInterest") or 0)


def _delta(option):
    d = option.get("delta")
    if d is None:
        return None
    try:
        return abs(float(d))
    except (TypeError, ValueError):
        return None


def _sell_price(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    return mid - MID_ADJUST_FRAC * (ask - bid)


def _buy_price(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    return mid + MID_ADJUST_FRAC * (ask - bid​​​​​​​​​​​​​​​​
