"""
itm.py
ITM Conversion scanner — own stock + sell ITM call + buy same-strike put.
Commission-aware, fillability-filtered, with primary + fallback pricing.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DTE_MIN              = 1
DTE_MAX              = 45
STRIKES_BELOW_SPOT   = 4
MID_ADJUST_FRAC      = 0.15
FALLBACK_STEP_FRAC   = 0.15
MIN_OI               = 50
MAX_SPREAD_PCT       = 0.40
COMMISSION_PER_CONTRACT = 1.30
MIN_LOCKED_AFTER_COMM_PER_CONTRACT = 5.0

_FREQ_DAYS = {
    "M": 30, "Q": 91, "S": 182, "A": 365, "W": 7,
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


def _spread_pct(option):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 99.0
    return (ask - bid) / mid


def _sell_price(option, extra_frac=0.0):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    return mid - (MID_ADJUST_FRAC + extra_frac) * (ask - bid)


def _buy_price(option, extra_frac=0.0):
    bid = _bid(option)
    ask = _ask(option)
    mid = (bid + ask) / 2.0
    return mid + (MID_ADJUST_FRAC + extra_frac) * (ask - bid)


def _compute_annual_div(fundamentals, spot):
    if not fundamentals:
        return 0.0
    annual_div = float(fundamentals.get("dividendAmount") or 0.0)
    if annual_div > 0:
        return annual_div
    div_yield = float(fundamentals.get("dividendYield") or 0.0)
    if div_yield <= 0:
        return 0.0
    if div_yield > 1:
        return (div_yield / 100.0) * spot
    return div_yield * spot


def _project_ex_div_dates(last_ex_div, freq, until):
    if not last_ex_div:
        return 0
    interval = _FREQ_DAYS.get(freq, 91)
    today = datetime.utcnow()
    next_div = last_ex_div + timedelta(days=interval)
    while next_div < today:
        next_div += timedelta(days=interval)
    count = 0
    while next_div <= until:
        count += 1
        next_div += timedelta(days=interval)
    return count


def _locked_and_apy(spot, strike, call_credit, put_cost, dte):
    net_credit = call_credit - put_cost
    gap = spot - strike
    commission_per_share = COMMISSION_PER_CONTRACT / 100.0
    locked = (net_credit - gap) - commission_per_share
    cost_basis = spot - net_credit
    if cost_basis <= 0 or dte <= 0:
        apy = 0.0
    else:
        apy = (locked / cost_basis) * (365.0 / dte) * 100.0
    return net_credit, locked, locked * 100.0, apy


class ItmScanner:
    def __init__(self, schwab_client, ticker_freqs=None):
        self.schwab = schwab_client
        self.ticker_freqs = ticker_freqs or {}

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

        annual_div = 0.0
        last_ex_div = None
        try:
            fundamentals = self.schwab.get_fundamentals(ticker)
            annual_div = _compute_annual_div(fundamentals, spot)
            last_ex_div_str = fundamentals.get("exDividendDate") or fundamentals.get("dividendDate")
            if last_ex_div_str:
                try:
                    last_ex_div = datetime.strptime(last_ex_div_str[:10], "%Y-%m-%d")
                except ValueError:
                    pass
        except Exception:
            pass

        call_map = chain.get("callExpDateMap", {})
        put_map = chain.get("putExpDateMap", {})
        if not call_map or not put_map:
            debug["empty_chain"] += 1
            return results, debug

        all_exp_dates = set(k.split(":")[0] for k in call_map) & \
                        set(k.split(":")[0] for k in put_map)

        min_locked_per_share = MIN_LOCKED_AFTER_COMM_PER_CONTRACT / 100.0

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

            common_strikes_below = []
            for s in calls.keys():
                try:
                    fs = float(s)
                    if fs < spot and s in puts:
                        common_strikes_below.append(fs)
                except ValueError:
                    pass

            common_strikes_below.sort(reverse=True)
            common_strikes_below = common_strikes_below[:STRIKES_BELOW_SPOT]

            for strike in common_strikes_below:
                debug["candidates"] += 1
                strike_str = next(
                    (s for s in calls.keys() if abs(float(s) - strike) < 0.001),
                    None,
                )
                if not strike_str or strike_str not in puts:
                    continue

                call_contracts = calls.get(strike_str, [])
                put_contracts = puts.get(strike_str, [])
                if not call_contracts or not put_contracts:
                    continue

                call_opt = call_contracts[0]
                put_opt = put_contracts[0]

                if not _has_market(call_opt):
                    debug["call_no_market"] += 1
                    continue
                if not _has_market(put_opt):
                    debug["put_no_market"] += 1
                    continue

                if _oi(call_opt) < MIN_OI:
                    debug["call_oi_low"] += 1
                    continue
                if _oi(put_opt) < MIN_OI:
                    debug["put_oi_low"] += 1
                    continue
                if _spread_pct(call_opt) > MAX_SPREAD_PCT:
                    debug["call_spread_wide"] += 1
                    continue
                if _spread_pct(put_opt) > MAX_SPREAD_PCT:
                    debug["put_spread_wide"] += 1
                    continue

                call_credit_p = _sell_price(call_opt)
                put_cost_p = _buy_price(put_opt)
                net_credit_p, locked_p, locked_total_p, apy_p = _locked_and_apy(
                    spot, strike, call_credit_p, put_cost_p, dte
                )

                if locked_p < min_locked_per_share:
                    debug["below_min_locked_after_comm"] += 1
                    continue

                call_credit_f = _sell_price(call_opt, extra_frac=FALLBACK_STEP_FRAC)
                put_cost_f = _buy_price(put_opt, extra_frac=FALLBACK_STEP_FRAC)
                net_credit_f, locked_f, locked_total_f, apy_f = _locked_and_apy(
                    spot, strike, call_credit_f, put_cost_f, dte
                )

                primary_debit = spot - call_credit_p + put_cost_p
                fallback_debit = spot - call_credit_f + put_cost_f

                num_ex_divs = _project_ex_div_dates(last_ex_div, freq, exp_dt)
                div_yield_pct = (annual_div / spot) * 100.0 if spot > 0 else 0.0

                debug["passed"] += 1
                results.append(dict(
                    ticker=ticker,
                    exp_date=exp_date,
                    dte=dte,
                    spot=round(spot, 2),
                    strike=strike,
                    call_credit=round(call_credit_p, 2),
                    put_cost=round(put_cost_p, 2),
                    net_credit=round(net_credit_p, 2),
                    gap=round(spot - strike, 2),
                    locked_profit=round(locked_p, 4),
                    locked_total=round(locked_total_p, 2),
                    locked_apy=round(apy_p, 1),
                    primary_debit=round(primary_debit, 2),
                    fallback_debit=round(fallback_debit, 2),
                    fallback_locked_total=round(locked_total_f, 2),
                    fallback_apy=round(apy_f, 1),
                    cost_basis=round(spot - net_credit_p, 2),
                    annual_div=round(annual_div, 2),
                    div_yield_pct=round(div_yield_pct, 2),
                    num_ex_divs=num_ex_divs,
                    freq=freq,
                    call_oi=_oi(call_opt),
                    put_oi=_oi(put_opt),
                    call_bid=_bid(call_opt),
                    call_ask=_ask(call_opt),
                    put_bid=_bid(put_opt),
                    put_ask=_ask(put_opt),
                ))

        return results, debug

    def scan_ticker_reverse(self, ticker):
        """
        Reverse mode: find strikes ABOVE spot where put is expensive relative
        to call (put_mid > call_mid). Same schema as scan_ticker so the existing
        _run_scan / format_summary pipeline works unchanged.
        Usage: /itm r
        """
        results = []
        debug = Counter()
        ticker = ticker.upper()
        freq = self.ticker_freqs.get(ticker, "Q")

        try:
            chain = self.schwab.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] reverse scan chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            debug["no_spot"] += 1
            return results, debug

        annual_div = 0.0
        last_ex_div = None
        try:
            fundamentals = self.schwab.get_fundamentals(ticker)
            annual_div = _compute_annual_div(fundamentals, spot)
            last_ex_div_str = fundamentals.get("exDividendDate") or fundamentals.get("dividendDate")
            if last_ex_div_str:
                try:
                    last_ex_div = datetime.strptime(last_ex_div_str[:10], "%Y-%m-%d")
                except ValueError:
                    pass
        except Exception:
            pass

        call_map = chain.get("callExpDateMap", {})
        put_map = chain.get("putExpDateMap", {})
        if not call_map or not put_map:
            debug["empty_chain"] += 1
            return results, debug

        all_exp_dates = set(k.split(":")[0] for k in call_map) & \
                        set(k.split(":")[0] for k in put_map)

        min_locked_per_share = MIN_LOCKED_AFTER_COMM_PER_CONTRACT / 100.0

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

            # Strikes ABOVE spot
            common_strikes_above = []
            for s in calls.keys():
                try:
                    fs = float(s)
                    if fs > spot and s in puts:
                        common_strikes_above.append(fs)
                except ValueError:
                    pass

            common_strikes_above.