"""
csp.py
Bull Put Credit Spread scanner — OTM short puts for stock acquisition at discount.

Goal: collect premium for selling OTM puts on quality dividend stocks.
- If stock stays above short strike → keep credit (pure income)
- If assigned → own stock at discount to current spot
"""

import logging
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

DTE_MIN              = 1
DTE_MAX              = 21
DELTA_MIN            = 0.20
DELTA_MAX            = 0.30
WIDTH                = 2.50
MID_ADJUST_FRAC      = 0.15
MIN_RETURN_PCT       = 5.0
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
    return mid + MID_ADJUST_FRAC * (ask - bid)


class CspScanner:
    def __init__(self, schwab_client, ticker_freqs=None):
        self.schwab = schwab_client
        self.ticker_freqs = ticker_freqs or {}

    def scan_ticker(self, ticker):
        results = []
        debug = Counter()
        ticker = ticker.upper()
        freq = self.ticker_freqs.get(ticker, "?")

        try:
            chain = self.schwab.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] option chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            debug["no_spot"] += 1
            return results, debug

        if spot > MAX_PRICE:
            debug["price_above_max"] += 1
            return results, debug

        annual_div = 0.0
        try:
            fundamentals = self.schwab.get_fundamentals(ticker)
            annual_div = float(fundamentals.get("dividendAmount") or 0.0)
            if annual_div <= 0:
                div_yield = float(fundamentals.get("dividendYield") or 0.0)
                annual_div = (div_yield / 100.0) * spot if div_yield > 1 else div_yield *
