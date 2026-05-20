"""
ritm.py
Reverse ITM Conversion scanner — opposite of /itm.

Setup: short stock + buy ITM put + sell same-strike call (strike ABOVE spot).
Single locked outcome regardless of where stock goes by expiration:
  - Stock < strike → put assigned, you BUY at strike to cover short
  - Stock >= strike → call exercised against you, you BUY at strike to cover short
  Either way: cover at strike. Locked outcome.

Locked profit = net_credit - (strike - spot) - dividends_owed - borrow_cost
  where net_credit = put_credit (selling ITM put) - call_cost (buying OTM call)
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DTE_MIN              = 1
DTE_MAX              = 14
STRIKES_ABOVE_SPOT   = 4
MID_ADJUST_FRAC      = 0.15
MIN_OI               = 10
MAX_SPREAD_PCT       = 0.75
ASSUMED_BORROW_RATE_APY = 1.0


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


class RitmScanner:
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

            common_strikes_above = []
            for s in calls.keys():
                try:
                    fs = float(s)
                    if fs > spot and s in puts:
                        common_strikes_above.append(fs)
                except ValueError:
                    pass

            common_strikes_above.sort()
            common_strikes_above = common_strikes_above[:STRIKES_ABOVE_SPOT]

            for strike in common_strikes_above:
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

                put_credit = _sell_price(put_opt)
                call_cost = _buy_price(call_opt)
                net_credit = put_credit - call_cost

                gap_above = strike - spot
                gross_locked = net_credit - gap_above

                num_ex_divs_in_window = _project_ex_div_dates(last_ex_div, freq, exp_dt)
                cycles_per_year = {
                    "M": 12, "Q": 4, "S": 2, "A": 1, "W": 52,
                }.get(freq, 4)
                expected_dividends_owed = 0.0
                if annual_div > 0 and num_ex_divs_in_window > 0:
                    expected_dividends_owed = (annual_div / cycles_per_year) * num_ex_divs_in_window

                borrow_cost = spot * (ASSUMED_BORROW_RATE_APY / 100.0) * (dte / 365.0)

                net_locked = gross_locked - expected_dividends_owed - borrow_cost

                if net_locked <= 0:
                    debug["non_positive_net_locked"] += 1
                    continue

                cost_basis = spot
                locked_apy = (net_locked / cost_basis) * (365.0 / dte) * 100.0

                div_yield_pct = (annual_div / spot) * 100.0 if spot > 0 else 0.0

                debug["passed"] += 1
                results.append(dict(
                    ticker=ticker,
                    exp_date=exp_date,
                    dte=dte,
                    spot=round(spot, 2),
                    strike=strike,
                    put_credit=round(put_credit, 2),
                    call_cost=round(call_cost, 2),
                    net_credit=round(net_credit, 2),
                    gap_above=round(gap_above, 2),
                    gross_locked=round(gross_locked, 2),
                    expected_dividends_owed=round(expected_dividends_owed, 2),
                    borrow_cost=round(borrow_cost, 2),
                    net_locked=round(net_locked, 2),
                    locked_apy=round(locked_apy, 1),
                    cost_basis=round(cost_basis, 2),
                    annual_div=round(annual_div, 2),
                    div_yield_pct=round(div_yield_pct, 2),
                    num_ex_divs=num_ex_divs_in_window,
                    freq=freq,
                    call_oi=_oi(call_opt),
                    put_oi=_oi(put_opt),
                ))

        return results, debug

    @staticmethod
    def format_hit(r):
        freq_label = {
            "M": "monthly", "Q": "quarterly", "W": "weekly",
            "S": "semi-annual", "A": "annual", "?": "unknown",
        }.get(r.get("freq", "?"), r.get("freq", "?"))
        div_line = ""
        if r['annual_div'] > 0 and r['num_ex_divs'] > 0:
            div_line = (
                f"  ⚠️ Dividends owed: ${r['expected_dividends_owed']}/sh "
                f"({r['num_ex_divs']} {freq_label} ex-div in window)\n"
            )
        return (
            f"🔄 *{r['ticker']}* @ ${r['spot']}\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 Buy  C ${r['strike']:g} @ ${r['call_cost']}\n"
            f"  🛡️ Sell P ${r['strike']:g} @ ${r['put_credit']}\n"
            f"  💵 Net credit: ${r['net_credit']}/sh · Gap above: ${r['gap_above']}/sh\n"
            f"  📊 Gross locked: ${r['gross_locked']}/sh\n"
            f"{div_line}"
            f"  💸 Borrow cost (~{ASSUMED_BORROW_RATE_APY:g}% APY): ${r['borrow_cost']}/sh\n"
            f"  📊 OI call/put: {r['call_oi']}/{r['put_oi']}\n"
            f"  🎯 Net locked: *${r['net_locked']}/sh* (*{r['locked_apy']}% APY*)"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, debug_totals=None):
        header = (
            f"🔄 *Reverse ITM Conversion Scan*\n"
            f"Tickers: {scanned} · ✅ {successful} scanned · ⚠️ {len(errors)} errored\n"
            f"Short stock + buy call + sell put · strike > spot · {DTE_MIN}-{DTE_MAX}d\n"
            f"OI ≥ {MIN_OI} · spread ≤ {int(MAX_SPREAD_PCT*100)}% · borrow ~{ASSUMED_BORROW_RATE_APY:g}%/yr\n"
            f"Positive net locked profit only · _Best at BOTTOM_\n"
        )
        if debug_totals:
            d = debug_totals
            header += (
                f"\n🔬 *Debug — candidates: {d.get('candidates', 0):,}*\n"
                f"  · call no market:   {d.get('call_no_market', 0):,}\n"
                f"  · put no market:    {d.get('put_no_market', 0):,}\n"
                f"  · call OI low:      {d.get('call_oi_low', 0):,}\n"
                f"  · put OI low:       {d.get('put_oi_low', 0):,}\n"
                f"  · call spread wide: {d.get('call_spread_wide', 0):,}\n"
                f"  · put spread wide:  {d.get('put_spread_wide', 0):,}\n"
                f"  · non-positive net: {d.get('non_positive_net_locked', 0):,}\n"
                f"  · ✅ passed:        {d.get('passed', 0):,}\n"
            )
        header += f"\nOpportunities: *{len(all_hits)}*\n"
        if errors:
            err_block = "\n".join(f"  • {e}" for e in errors[:20])
            if len(err_block) > 1500:
                tickers_only = ", ".join(e.split(":")[0] for e in errors)
                err_block = f"  {tickers_only}\n_(use_ `/logs` _for details)_"
            header += f"\n⚠️ *Errors:*\n{err_block}\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"

        if not all_hits:
            return [header + "_No positive-locked-profit reverse conversions found._"]

        all_hits.sort(key=lambda r: r["locked_apy"])

        chunks, current = [], header
        for hit in all_hits:
            block = RitmScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks
