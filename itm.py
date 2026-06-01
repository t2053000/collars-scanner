"""
itm.py
ITM Conversion scanner — own stock + sell ITM call + buy same-strike put.
Commission-aware, fillability-filtered, with primary + fallback pricing.
Includes reverse mode (scan_ticker_reverse) for /itm r.

Sorting: best APY at bottom (shown last = closest to message field on phone).
Ex-div in window:
  - /itm normal: flagged, deprioritized by div_yield_apy_pts
  - /itm r:      flagged with red warning, deprioritized by 25 APY points
                 (you are SHORT stock so you PAY the dividend)
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DTE_MIN                              = 1
DTE_MAX                              = 45
STRIKES_BELOW_SPOT                   = 4
MID_ADJUST_FRAC                      = 0.15
FALLBACK_STEP_FRAC                   = 0.15
MIN_OI                               = 50
MAX_SPREAD_PCT                       = 0.40
COMMISSION_PER_CONTRACT              = 1.30
MIN_LOCKED_AFTER_COMM_PER_CONTRACT   = 5.0
HTB_SHORT_INT_THRESHOLD              = 0.20
REVERSE_EX_DIV_APY_PENALTY           = 25.0   # APY points subtracted from sort key

_FREQ_DAYS = {"M": 30, "Q": 91, "S": 182, "A": 365, "W": 7}


# ---------------------------------------------------------------------------
# Option helpers
# ---------------------------------------------------------------------------

def _has_market(option):
    return (option.get("bid") or 0.0) > 0 and (option.get("ask") or 0.0) > 0

def _bid(option):
    return float(option.get("bid") or 0.0)

def _ask(option):
    return float(option.get("ask") or 0.0)

def _oi(option):
    return int(option.get("openInterest") or 0)

def _spread_pct(option):
    bid, ask = _bid(option), _ask(option)
    mid = (bid + ask) / 2.0
    return 99.0 if mid <= 0 else (ask - bid) / mid

def _sell_price(option, extra_frac=0.0):
    bid, ask = _bid(option), _ask(option)
    mid = (bid + ask) / 2.0
    return mid - (MID_ADJUST_FRAC + extra_frac) * (ask - bid)

def _buy_price(option, extra_frac=0.0):
    bid, ask = _bid(option), _ask(option)
    mid = (bid + ask) / 2.0
    return mid + (MID_ADJUST_FRAC + extra_frac) * (ask - bid)


# ---------------------------------------------------------------------------
# Fundamentals helpers
# ---------------------------------------------------------------------------

def _compute_annual_div(fundamentals, spot):
    if not fundamentals:
        return 0.0
    annual_div = float(fundamentals.get("dividendAmount") or 0.0)
    if annual_div > 0:
        return annual_div
    div_yield = float(fundamentals.get("dividendYield") or 0.0)
    if div_yield <= 0:
        return 0.0
    return (div_yield / 100.0 if div_yield > 1 else div_yield) * spot


def _get_next_ex_div(fundamentals) -> str:
    """Return next ex-div date as 'YYYY-MM-DD' or '' — fetched live from Schwab."""
    if not fundamentals:
        return ""
    for field in ("exDividendDate", "dividendDate", "nextDividendDate",
                  "exDivDate", "nextExDivDate"):
        val = fundamentals.get(field)
        if val:
            try:
                return str(val)[:10]
            except Exception:
                continue
    return ""


def _get_short_interest(fundamentals) -> float:
    """Short interest as fraction of float (e.g. 0.25 = 25%)."""
    if not fundamentals:
        return 0.0
    for field in ("shortIntToFloat", "shortInterestRatio", "shortPercent",
                  "shortPercentOfFloat", "shortInterest"):
        val = fundamentals.get(field)
        if val is not None:
            try:
                v = float(val)
                return v / 100.0 if v > 1.0 else v
            except (ValueError, TypeError):
                continue
    return 0.0


def _ex_div_before_expiry(next_ex_div_date: str, exp_date: str) -> bool:
    """True if the live ex-div date falls on or before option expiry."""
    if not next_ex_div_date or not exp_date:
        return False
    try:
        return (datetime.strptime(next_ex_div_date[:10], "%Y-%m-%d") <=
                datetime.strptime(exp_date[:10], "%Y-%m-%d"))
    except ValueError:
        return False


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


# ---------------------------------------------------------------------------
# Sort key
# ---------------------------------------------------------------------------

def _sort_key_normal(hit: dict) -> float:
    """
    /itm normal: penalize ex-div-in-window hits by div yield in APY points.
    Higher = better = shown later (best at bottom).
    """
    apy = hit.get("locked_apy") or 0.0
    if hit.get("ex_div_in_window"):
        spot = hit.get("spot") or 1.0
        annual_div = hit.get("annual_div") or 0.0
        apy -= (annual_div / spot) * 100.0
    return apy


def _sort_key_reverse(hit: dict) -> float:
    """
    /itm r: penalize ex-div-in-window hits by fixed 25 APY points.
    Short stock means you PAY the dividend — much more harmful.
    """
    apy = hit.get("locked_apy") or 0.0
    if hit.get("ex_div_in_window"):
        apy -= REVERSE_EX_DIV_APY_PENALTY
    return apy


# ---------------------------------------------------------------------------
# ItmScanner
# ---------------------------------------------------------------------------

class ItmScanner:
    def __init__(self, schwab_client, ticker_freqs=None):
        self.schwab = schwab_client
        self.ticker_freqs = ticker_freqs or {}

    def _fetch_fundamentals(self, ticker, spot=0.0):
        annual_div = 0.0
        last_ex_div = None
        next_ex_div_date = ""
        short_int = 0.0
        try:
            fundamentals = self.schwab.get_fundamentals(ticker)
            annual_div = _compute_annual_div(fundamentals, spot)
            next_ex_div_date = _get_next_ex_div(fundamentals)
            short_int = _get_short_interest(fundamentals)
            if next_ex_div_date:
                try:
                    last_ex_div = datetime.strptime(next_ex_div_date[:10], "%Y-%m-%d")
                except ValueError:
                    pass
        except Exception:
            pass
        return annual_div, last_ex_div, next_ex_div_date, short_int

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

        annual_div, last_ex_div, next_ex_div_date, short_int = \
            self._fetch_fundamentals(ticker, spot)
        htb = short_int >= HTB_SHORT_INT_THRESHOLD

        call_map = chain.get("callExpDateMap", {})
        put_map  = chain.get("putExpDateMap",  {})
        if not call_map or not put_map:
            debug["empty_chain"] += 1
            return results, debug

        all_exp_dates = (set(k.split(":")[0] for k in call_map) &
                         set(k.split(":")[0] for k in put_map))
        min_locked = MIN_LOCKED_AFTER_COMM_PER_CONTRACT / 100.0

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
            pk = next((k for k in put_map  if k.startswith(exp_date + ":")), None)
            if not ck or not pk:
                continue

            calls = call_map[ck]
            puts  = put_map[pk]

            strikes_below = []
            for s in calls:
                try:
                    fs = float(s)
                    if fs < spot and s in puts:
                        strikes_below.append(fs)
                except ValueError:
                    pass
            strikes_below.sort(reverse=True)
            strikes_below = strikes_below[:STRIKES_BELOW_SPOT]

            for strike in strikes_below:
                debug["candidates"] += 1
                strike_str = next(
                    (s for s in calls if abs(float(s) - strike) < 0.001), None)
                if not strike_str or strike_str not in puts:
                    continue

                call_opt = (calls.get(strike_str) or [{}])[0]
                put_opt  = (puts.get(strike_str)  or [{}])[0]

                if not _has_market(call_opt): debug["call_no_market"] += 1; continue
                if not _has_market(put_opt):  debug["put_no_market"]  += 1; continue
                if _oi(call_opt) < MIN_OI:    debug["call_oi_low"]    += 1; continue
                if _oi(put_opt)  < MIN_OI:    debug["put_oi_low"]     += 1; continue
                if _spread_pct(call_opt) > MAX_SPREAD_PCT: debug["call_spread_wide"] += 1; continue
                if _spread_pct(put_opt)  > MAX_SPREAD_PCT: debug["put_spread_wide"]  += 1; continue

                call_credit_p = _sell_price(call_opt)
                put_cost_p    = _buy_price(put_opt)
                net_credit_p, locked_p, locked_total_p, apy_p = \
                    _locked_and_apy(spot, strike, call_credit_p, put_cost_p, dte)

                if locked_p < min_locked:
                    debug["below_min_locked_after_comm"] += 1
                    continue

                call_credit_f = _sell_price(call_opt, extra_frac=FALLBACK_STEP_FRAC)
                put_cost_f    = _buy_price(put_opt,   extra_frac=FALLBACK_STEP_FRAC)
                _, locked_f, locked_total_f, apy_f = \
                    _locked_and_apy(spot, strike, call_credit_f, put_cost_f, dte)

                primary_debit  = spot - call_credit_p + put_cost_p
                fallback_debit = spot - call_credit_f + put_cost_f
                div_yield_pct  = (annual_div / spot * 100.0) if spot > 0 else 0.0
                num_ex_divs    = _project_ex_div_dates(last_ex_div, freq, exp_dt)
                in_window      = _ex_div_before_expiry(next_ex_div_date, exp_date)

                debug["passed"] += 1
                results.append(dict(
                    ticker=ticker, exp_date=exp_date, dte=dte,
                    spot=round(spot, 2), strike=strike,
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
                    next_ex_div_date=next_ex_div_date,
                    ex_div_in_window=in_window,
                    short_int=round(short_int, 4),
                    htb=htb,
                    freq=freq,
                    call_oi=_oi(call_opt), put_oi=_oi(put_opt),
                    call_bid=_bid(call_opt), call_ask=_ask(call_opt),
                    put_bid=_bid(put_opt),  put_ask=_ask(put_opt),
                    reverse=False,
                ))

        return results, debug

    def scan_ticker_reverse(self, ticker):
        """
        /itm r: strikes ABOVE spot where put_mid > call_mid.
        Short stock + sell put + buy call.
        Ex-div in window is penalized 25 APY pts in sort (you PAY the dividend).
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

        annual_div, last_ex_div, next_ex_div_date, short_int = \
            self._fetch_fundamentals(ticker, spot)
        htb = short_int >= HTB_SHORT_INT_THRESHOLD

        call_map = chain.get("callExpDateMap", {})
        put_map  = chain.get("putExpDateMap",  {})
        if not call_map or not put_map:
            debug["empty_chain"] += 1
            return results, debug

        all_exp_dates = (set(k.split(":")[0] for k in call_map) &
                         set(k.split(":")[0] for k in put_map))
        min_locked = MIN_LOCKED_AFTER_COMM_PER_CONTRACT / 100.0

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
            pk = next((k for k in put_map  if k.startswith(exp_date + ":")), None)
            if not ck or not pk:
                continue

            calls = call_map[ck]
            puts  = put_map[pk]

            strikes_above = []
            for s in calls:
                try:
                    fs = float(s)
                    if fs > spot and s in puts:
                        strikes_above.append(fs)
                except ValueError:
                    pass
            strikes_above.sort()
            strikes_above = strikes_above[:STRIKES_BELOW_SPOT]

            for strike in strikes_above:
                debug["candidates"] += 1
                strike_str = next(
                    (s for s in calls if abs(float(s) - strike) < 0.001), None)
                if not strike_str or strike_str not in puts:
                    continue

                call_opt = (calls.get(strike_str) or [{}])[0]
                put_opt  = (puts.get(strike_str)  or [{}])[0]

                if not _has_market(call_opt): debug["call_no_market"] += 1; continue
                if not _has_market(put_opt):  debug["put_no_market"]  += 1; continue
                if _oi(call_opt) < MIN_OI:    debug["call_oi_low"]    += 1; continue
                if _oi(put_opt)  < MIN_OI:    debug["put_oi_low"]     += 1; continue
                if _spread_pct(call_opt) > MAX_SPREAD_PCT: debug["call_spread_wide"] += 1; continue
                if _spread_pct(put_opt)  > MAX_SPREAD_PCT: debug["put_spread_wide"]  += 1; continue

                put_mid  = (_bid(put_opt)  + _ask(put_opt))  / 2.0
                call_mid = (_bid(call_opt) + _ask(call_opt)) / 2.0
                if put_mid <= call_mid:
                    debug["no_put_skew"] += 1
                    continue

                put_credit_p = _sell_price(put_opt)
                call_cost_p  = _buy_price(call_opt)
                net_credit_p = put_credit_p - call_cost_p

                gap = strike - spot
                commission_per_share = COMMISSION_PER_CONTRACT / 100.0
                locked_p = (net_credit_p - gap) - commission_per_share
                if locked_p < min_locked:
                    debug["below_min_locked_after_comm"] += 1
                    continue

                apy_p = (locked_p / spot) * (365.0 / dte) * 100.0 \
                    if spot > 0 and dte > 0 else 0.0

                put_credit_f = _sell_price(put_opt,  extra_frac=FALLBACK_STEP_FRAC)
                call_cost_f  = _buy_price(call_opt,  extra_frac=FALLBACK_STEP_FRAC)
                net_credit_f = put_credit_f - call_cost_f
                locked_f     = (net_credit_f - gap) - commission_per_share
                apy_f = (locked_f / spot) * (365.0 / dte) * 100.0 \
                    if locked_f > 0 and spot > 0 and dte > 0 else 0.0

                div_yield_pct = (annual_div / spot * 100.0) if spot > 0 else 0.0
                num_ex_divs   = _project_ex_div_dates(last_ex_div, freq, exp_dt)
                in_window     = _ex_div_before_expiry(next_ex_div_date, exp_date)

                debug["passed"] += 1
                results.append(dict(
                    ticker=ticker, exp_date=exp_date, dte=dte,
                    spot=round(spot, 2), strike=strike,
                    call_credit=round(call_cost_p, 2),
                    put_cost=round(put_credit_p, 2),
                    net_credit=round(net_credit_p, 2),
                    gap=round(gap, 2),
                    locked_profit=round(locked_p, 4),
                    locked_total=round(locked_p * 100, 2),
                    locked_apy=round(apy_p, 1),
                    primary_debit=round(spot - net_credit_p, 2),
                    fallback_debit=round(spot - net_credit_f, 2),
                    fallback_locked_total=round(locked_f * 100, 2),
                    fallback_apy=round(apy_f, 1),
                    cost_basis=round(spot, 2),
                    annual_div=round(annual_div, 2),
                    div_yield_pct=round(div_yield_pct, 2),
                    num_ex_divs=num_ex_divs,
                    next_ex_div_date=next_ex_div_date,
                    ex_div_in_window=in_window,
                    short_int=round(short_int, 4),
                    htb=htb,
                    freq=freq,
                    call_oi=_oi(call_opt), put_oi=_oi(put_opt),
                    call_bid=_bid(call_opt), call_ask=_ask(call_opt),
                    put_bid=_bid(put_opt),  put_ask=_ask(put_opt),
                    reverse=True,
                ))

        return results, debug

    @staticmethod
    def format_hit(r):
        freq_label = {
            "M": "monthly", "Q": "quarterly", "W": "weekly",
            "S": "semi-annual", "A": "annual", "?": "unknown",
        }.get(r.get("freq", "?"), r.get("freq", "?"))

        # Ex-div warning — red for reverse (you pay), amber for normal (early assignment risk)
        ex_div_line = ""
        if r.get("ex_div_in_window"):
            ex_date = r.get("next_ex_div_date", "")
            if r.get("reverse"):
                ex_div_line = (
                    f"  🚨 EX-DIV {ex_date} BEFORE EXPIRY — you PAY dividend (short stock)\n"
                )
            else:
                ex_div_line = (
                    f"  ⚠️ Ex-div {ex_date} before expiry — early assignment risk\n"
                )
        elif r.get("annual_div", 0) > 0:
            ex_date = r.get("next_ex_div_date", "")
            ex_date_str = f" · next ex-div: {ex_date}" if ex_date else ""
            ex_div_line = (
                f"  💸 Div: ${r['annual_div']}/yr ({r['div_yield_pct']}%) · "
                f"paid {freq_label} · {r['num_ex_divs']} ex-div in window{ex_date_str}\n"
            )

        htb_line = ""
        if r.get("htb"):
            pct = round(r.get("short_int", 0) * 100, 1)
            htb_line = f"  ⚠️ HTB? Short interest {pct}% of float\n"

        return (
            f"🔒 *{r['ticker']}* @ ${r['spot']}\n"
            f"  📅 {r['exp_date']} ({r['dte']}d)\n"
            f"  📞 Sell C ${r['strike']:g} @ ${r['call_credit']}\n"
            f"  🛡️ Buy  P ${r['strike']:g} @ ${r['put_cost']}\n"
            f"  💵 Net credit: ${r['net_credit']}/sh · Gap: ${r['gap']}/sh\n"
            f"  💳 Pay ${r['primary_debit']}/sh net → *{r['locked_apy']}% APY* (${r['locked_total']:.0f})\n"
            f"  🔄 If unfilled, pay ${r['fallback_debit']}/sh → {r['fallback_apy']}% APY (${r['fallback_locked_total']:.0f})\n"
            f"{ex_div_line}"
            f"{htb_line}"
            f"  📊 OI call/put: {r['call_oi']}/{r['put_oi']}"
        )

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, debug_totals=None):
        header = (
            f"🔒 *ITM Conversion Scan*\n"
            f"Tickers: {scanned} · ✅ {successful} scanned · ⚠️ {len(errors)} errored\n"
            f"Strike < spot · {DTE_MIN}-{DTE_MAX}d · OI ≥ {MIN_OI} both legs · "
            f"spread ≤ {int(MAX_SPREAD_PCT*100)}%\n"
            f"Min ${MIN_LOCKED_AFTER_COMM_PER_CONTRACT:g} profit/spread after "
            f"${COMMISSION_PER_CONTRACT:g} comm\n"
            f"_Best at BOTTOM_\n"
        )
        if debug_totals:
            d = debug_totals
            header += (
                f"\n🔬 *Debug — candidates: {d.get('candidates', 0):,}*\n"
                f"  · call no market:   {d.get('call_no_market', 0):,}\n"
                f"  · put no market:    {d.get('put_no_market', 0):,}\n"
                f"  · call OI < {MIN_OI}:    {d.get('call_oi_low', 0):,}\n"
                f"  · put OI < {MIN_OI}:     {d.get('put_oi_low', 0):,}\n"
                f"  · call spread wide: {d.get('call_spread_wide', 0):,}\n"
                f"  · put spread wide:  {d.get('put_spread_wide', 0):,}\n"
                f"  · below min profit: {d.get('below_min_locked_after_comm', 0):,}\n"
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
            return [header + "_No fillable conversions found._"]

        # Sort using appropriate key depending on whether hits are from reverse scan
        # (mixed runs shouldn't happen but handle gracefully)
        def sort_key(r):
            apy = r.get("locked_apy") or 0.0
            if r.get("ex_div_in_window"):
                if r.get("reverse"):
                    apy -= REVERSE_EX_DIV_APY_PENALTY
                else:
                    spot = r.get("spot") or 1.0
                    annual_div = r.get("annual_div") or 0.0
                    apy -= (annual_div / spot) * 100.0
            return apy

        all_hits.sort(key=sort_key)

        chunks, current = [], header
        for hit in all_hits:
            block = ItmScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks