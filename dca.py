"""
dca.py
Dividend Collar Arbitrage scanner.

Score formula (Option C - early-assignment aware):
  gap            = max(spot - strike, 0)
  safety_dollars = net_premium - gap

  if safety_dollars < 0:
      # Likely called away at first ex-div → only 1 dividend collected
      expected_div = annual_div / cycles_per_year
  else:
      # Held to expiry → collect all ex-divs in option life
      expected_div = annual_div * (dte / 365)

  score_dollars = safety_dollars + expected_div
  score_apy     = (score_dollars / spot) * (365/dte) * 100
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DTE_MIN              = 90
DTE_MAX              = 730
MIN_STRIKE_PCT_SPOT  = 0.80
MAX_STRIKE_PCT_SPOT  = 1.00
MID_ADJUST_FRAC      = 0.15
MAX_PRICE            = 43.0


_FREQ_DAYS = {
    "M": 30,
    "Q": 91,
    "S": 182,
    "A": 365,
    "W": 7,
}

_CYCLES_PER_YEAR = {
    "M": 12,
    "Q": 4,
    "S": 2,
    "A": 1,
    "W": 52,
}


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


def _project_ex_div_dates(last_ex_div, freq, until):
    interval = _FREQ_DAYS.get(freq, 91)
    today = datetime.utcnow()

    if last_ex_div:
        next_div = last_ex_div + timedelta(days=interval)
        while next_div < today:
            next_div += timedelta(days=interval)
    else:
        next_div = today + timedelta(days=interval // 2)

    count = 0
    while next_div <= until:
        count += 1
        next_div += timedelta(days=interval)
    return count


class DcaScanner:
    def __init__(self, schwab_client, ticker_freqs):
        self.schwab = schwab_client
        self.ticker_freqs = ticker_freqs

    def scan_ticker(self, ticker):
        results = []
        debug = Counter()
        ticker = ticker.upper()
        freq = self.ticker_freqs.get(ticker, "Q")

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

        try:
            fundamentals = self.schwab.get_fundamentals(ticker)
        except Exception as e:
            logger.warning(f"[{ticker}] fundamentals fetch failed: {e}")
            fundamentals = {}

        annual_div = float(fundamentals.get("dividendAmount") or 0.0)
        if annual_div <= 0:
            div_yield = float(fundamentals.get("dividendYield") or 0.0)
            annual_div = (div_yield / 100.0) * spot if div_yield > 1 else div_yield * spot

        if annual_div <= 0:
            debug["no_dividend_data"] += 1
            return results, debug

        last_ex_div_str = fundamentals.get("exDividendDate") or fundamentals.get("dividendDate")
        last_ex_div = None
        if last_ex_div_str:
            try:
                last_ex_div = datetime.strptime(last_ex_div_str[:10], "%Y-%m-%d")
            except ValueError:
                pass

        if last_ex_div is None:
            debug["ex_div_estimated"] += 1

        call_map = chain.get("callExpDateMap", {})
        put_map = chain.get("putExpDateMap", {})
        if not call_map or not put_map:
            debug["empty_chain"] += 1
            return results, debug

        strike_floor = spot * MIN_STRIKE_PCT_SPOT
        strike_ceil = spot * MAX_STRIKE_PCT_SPOT

        all_exp_dates = set(k.split(":")[0] for k in call_map) & \
                        set(k.split(":")[0] for k in put_map)

        for exp_date in sorted(all_exp_dates):
            try:
                exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            except ValueError:
                continue
            dte = (exp_dt - datetime.utcnow()).days
            if dte < DTE_MIN or dte > DTE_MAX:
                debug["dte_out_of_range"] += 1
                continue

            ck = next((k for k in call_map if k.startswith(exp_date + ":")), None)
            pk = next((k for k in put_map if k.startswith(exp_date + ":")), None)
            if not ck or not pk:
                continue

            calls = call_map[ck]
            puts = put_map[pk]

            common_strikes = set()
            for s in calls.keys():
                try:
                    fs = float(s)
                    if strike_floor
