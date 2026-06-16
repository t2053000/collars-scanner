# itm_ibkr.py
"""
itm_ibkr.py
Reverse ITM conversion scanner using IBKR for market data.
Identical logic to itm.py scan_ticker_reverse — only data source changes.
Order execution stays on Schwab (unchanged).

Triggered by /itmib r command in bot.py.
Zero changes to existing itm.py, bot.py execution path, or orders.py.

Architecture:
  Scanning  → IBKR (better data, real borrow rates, short availability)
  Execution → Schwab (unchanged)
"""

import logging
from collections import Counter
from datetime import datetime, timedelta

from ibkr_client import IbkrClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — identical to itm.py for consistency
# ---------------------------------------------------------------------------

DTE_MIN                            = 1
REVERSE_DTE_MAX                    = 14
STRIKES_ABOVE_SPOT_REVERSE         = 2
MID_ADJUST_FRAC                    = 0.15
FALLBACK_STEP_FRAC                 = 0.15
MIN_OI                             = 50
MAX_SPREAD_PCT                     = 0.40
COMMISSION_PER_CONTRACT            = 1.30
MIN_LOCKED_AFTER_COMM_PER_CONTRACT = 5.0
HTB_SHORT_INT_THRESHOLD            = 0.20
REVERSE_EX_DIV_APY_PENALTY         = 25.0
REVERSE_BORROW_RATE                = 0.20  # fallback if live rate unavailable

_FREQ_DAYS = {"M": 30, "Q": 91, "S": 182, "A": 365, "W": 7}


# ---------------------------------------------------------------------------
# Option helpers — identical to itm.py
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

def _ex_div_before_expiry(next_ex_div_date: str, exp_date: str) -> bool:
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

def _sort_key(hit: dict) -> float:
    apy = hit.get("locked_apy") or 0.0
    if hit.get("ex_div_in_window"):
        if hit.get("reverse"):
            apy -= REVERSE_EX_DIV_APY_PENALTY
        else:
            spot = hit.get("spot") or 1.0
            annual_div = hit.get("annual_div") or 0.0
            apy -= (annual_div / spot) * 100.0
    return apy


# ---------------------------------------------------------------------------
# ItmIbkrScanner
# ---------------------------------------------------------------------------

class ItmIbkrScanner:
    """
    Reverse ITM conversion scanner using IBKR for market data.
    Drop-in replacement for ItmScanner.scan_ticker_reverse
    but with IBKR option chains instead of Schwab.

    scan_ticker() is a stub that raises — only reverse is supported here.
    scan_ticker_reverse() is the main entry point.

    format_hit() and format_summary() are identical to ItmScanner
    so bot.py can use them interchangeably.
    """

    def __init__(self, ibkr_client: IbkrClient, ticker_freqs=None):
        self.ibkr         = ibkr_client
        self.ticker_freqs = ticker_freqs or {}

    def scan_ticker(self, ticker):
        """Stub — not used for IBKR scanner (reverse only)."""
        raise NotImplementedError("ItmIbkrScanner only supports scan_ticker_reverse")

    def scan_ticker_reverse(self, ticker: str):
        """
        Reverse ITM scan using IBKR option chain data.
        Same logic as itm.py scan_ticker_reverse — only data source differs.
        IBKR advantages over Schwab:
          - Live borrow rate (future: replace REVERSE_BORROW_RATE with real rate)
          - Short availability check before scanning
          - Faster chain fetch for narrow strike range
        """
        results = []
        debug   = Counter()
        ticker  = ticker.upper()
        freq    = self.ticker_freqs.get(ticker, "Q")

        # Check short availability before fetching full chain
        try:
            avail = self.ibkr.get_short_availability(ticker)
            if not avail.get("available", True):
                debug["not_shortable"] += 1
                logger.info(f"[{ticker}] not shortable per IBKR — skipping")
                return results, debug

            # Use live borrow rate if available, else fall back to constant
            live_borrow_rate = avail.get("borrow_rate_pct", 0.0)
            borrow_rate = (live_borrow_rate / 100.0) if live_borrow_rate > 0 \
                else REVERSE_BORROW_RATE
        except Exception as e:
            logger.warning(f"[{ticker}] short availability check failed: {e}")
            borrow_rate = REVERSE_BORROW_RATE

        # Fetch option chain from IBKR
        try:
            chain = self.ibkr.get_option_chain(ticker)
        except Exception as e:
            logger.error(f"[{ticker}] IBKR chain fetch failed: {e}")
            raise

        spot = chain.get("underlyingPrice")
        if not spot or spot <= 0:
            debug["no_spot"] += 1
            return results, debug

        # No fundamentals from IBKR — use defaults
        annual_div       = 0.0
        last_ex_div      = None
        next_ex_div_date = ""
        short_int        = 0.0
        htb              = False

        call_map = chain.get("callExpDateMap", {})
        put_map  = chain.get("putExpDateMap",  {})
        if not call_map or not put_map:
            debug["empty_chain"] += 1
            return results, debug

        all_exp_dates = (set(k.split(":")[0] for k in call_map) &
                         set(k.split(":")[0] for k in put_map))
        min_locked = MIN_LOCKED_AFTER_COMM_PER_CONTRACT / 100.0

        call_key_map = {k.split(":")[0]: k for k in call_map}
        put_key_map  = {k.split(":")[0]: k for k in put_map}

        for exp_date in sorted(all_exp_dates):
            try:
                exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
            except ValueError:
                continue
            dte = (exp_dt - datetime.utcnow()).days
            if dte < DTE_MIN or dte > REVERSE_DTE_MAX:
                debug["dte_out_of_range"] += 1
                continue

            ck = call_key_map.get(exp_date)
            pk = put_key_map.get(exp_date)
            if not ck or not pk:
                continue

            calls = call_map[ck]
            puts  = put_map[pk]

            borrow_cost = spot * borrow_rate * (dte / 365.0)

            strikes_above = []
            for s in calls:
                try:
                    fs = float(s)
                    if fs > spot and s in puts:
                        strikes_above.append(fs)
                except ValueError:
                    pass
            strikes_above.sort()
            strikes_above = strikes_above[:STRIKES_ABOVE_SPOT_REVERSE]

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

                in_window = _ex_div_before_expiry(next_ex_div_date, exp_date)

                div_cost = 0.0
                if in_window and annual_div > 0:
                    cycles = {"M": 12, "Q": 4, "S": 2, "A": 1, "W": 52}.get(freq, 4)
                    div_cost = annual_div / cycles

                locked_p = (net_credit_p - gap - commission_per_share
                            - borrow_cost - div_cost)
                if locked_p < min_locked:
                    debug["below_min_locked_after_comm"] += 1
                    continue

                apy_p = (locked_p / spot) * (365.0 / dte) * 100.0 \
                    if spot > 0 and dte > 0 else 0.0

                put_credit_f = _sell_price(put_opt,  extra_frac=FALLBACK_STEP_FRAC)
                call_cost_f  = _buy_price(call_opt,  extra_frac=FALLBACK_STEP_FRAC)
                net_credit_f = put_credit_f - call_cost_f
                locked_f     = (net_credit_f - gap - commission_per_share
                                - borrow_cost - div_cost)
                apy_f = (locked_f / spot) * (365.0 / dte) * 100.0 \
                    if locked_f > 0 and spot > 0 and dte > 0 else 0.0

                div_yield_pct = 0.0
                num_ex_divs   = 0

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
                    borrow_cost=round(borrow_cost, 4),
                    div_cost=round(div_cost, 4),
                    data_source="ibkr",
                ))

        return results, debug

    @staticmethod
    def format_hit(r):
        """Identical to ItmScanner.format_hit — reused directly."""
        from itm import ItmScanner
        return ItmScanner.format_hit(r)

    @staticmethod
    def format_summary(all_hits, scanned, successful, errors, debug_totals=None):
        header = (
            f"🔄 *IBKR Reverse ITM Scan*\n"
            f"Tickers: {scanned} · ✅ {successful} scanned · ⚠️ {len(errors)} errored\n"
            f"Strike > spot · {DTE_MIN}-{REVERSE_DTE_MAX}d · OI ≥ {MIN_OI} both legs · "
            f"spread ≤ {int(MAX_SPREAD_PCT*100)}%\n"
            f"Min ${MIN_LOCKED_AFTER_COMM_PER_CONTRACT:g} profit after "
            f"${COMMISSION_PER_CONTRACT:g} comm · Data: IBKR\n"
            f"_Best at BOTTOM_\n"
        )
        if debug_totals:
            d = debug_totals
            header += (
                f"\n🔬 *Debug — candidates: {d.get('candidates', 0):,}*\n"
                f"  · not shortable:    {d.get('not_shortable', 0):,}\n"
                f"  · call no market:   {d.get('call_no_market', 0):,}\n"
                f"  · put no market:    {d.get('put_no_market', 0):,}\n"
                f"  · call OI < {MIN_OI}:    {d.get('call_oi_low', 0):,}\n"
                f"  · put OI < {MIN_OI}:     {d.get('put_oi_low', 0):,}\n"
                f"  · no put skew:      {d.get('no_put_skew', 0):,}\n"
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
            return [header + "_No IBKR reverse ITM opportunities found._"]

        all_hits.sort(key=_sort_key)

        chunks, current = [], header
        for hit in all_hits:
            block = ItmIbkrScanner.format_hit(hit) + "\n\n"
            if len(current) + len(block) > 3800:
                chunks.append(current.rstrip())
                current = ""
            current += block
        if current.strip():
            chunks.append(current.rstrip())
        return chunks
