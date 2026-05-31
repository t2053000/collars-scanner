"""
ritm.py
Reverse ITM conversion scanner.

Setup: SHORT 100 shares + BUY 1 call (strike > spot) + SELL 1 put (same strike)
At expiry: position locks at strike regardless of underlying.
Locked profit per share = net_credit_received - (strike - spot) - borrow_cost - dividend_cost

Requires margin account with short-sale and naked-put approval on Schwab.
Borrow rate assumed at 25% APR (conservative — small caps with put skew
that show up in /ritm hits often have high borrow).
"""

import logging
from datetime import datetime
from schwab_client import SchwabClient
from github_store import load_tickers

logger = logging.getLogger(__name__)

# --- Filter constants ---
DTE_MIN = 1
DTE_MAX = 14
BORROW_RATE_PCT = 25.0  # conservative annualized borrow rate
COMMISSION_PER_CONTRACT = 1.30
MIN_OI = 50
MAX_SPREAD_PCT = 0.40
MIN_LOCKED_AFTER_COMM_PER_CONTRACT = 5.0
FALLBACK_STEP_FRAC = 0.15  # how much worse the fallback price is vs mid
MAX_HITS = 50


def _safe_mid(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def _spread_pct(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return 1.0
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 1.0
    return (ask - bid) / mid


def scan_ritm(tickers=None):
    """
    Scan tickers for reverse-conversion opportunities.
    Returns list of hit dicts, sorted ascending by locked_apy (best last).
    """
    client = SchwabClient()
    if tickers is None:
        tickers = load_tickers("tickers.txt")

    hits = []
    today = datetime.now().date()

    for ticker in tickers:
        ticker = ticker.strip().upper()
        if not ticker:
            continue

        try:
            quote = client.get_quote(ticker)
            spot = quote.get("last") or quote.get("mark")
            if not spot or spot <= 0:
                continue

            chain = client.get_option_chain(ticker)
            if not chain:
                continue

            call_map = chain.get("callExpDateMap", {})
            put_map = chain.get("putExpDateMap", {})

            for exp_key in call_map:
                if exp_key not in put_map:
                    continue

                exp_date_str = exp_key.split(":")[0]
                exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
                if dte < DTE_MIN or dte > DTE_MAX:
                    continue

                for strike_str in call_map[exp_key]:
                    if strike_str not in put_map[exp_key]:
                        continue
                    strike = float(strike_str)

                    # /ritm: strike must be ABOVE spot
                    if strike <= spot:
                        continue

                    call_data = call_map[exp_key][strike_str][0]
                    put_data = put_map[exp_key][strike_str][0]

                    call_bid = call_data.get("bid")
                    call_ask = call_data.get("ask")
                    put_bid = put_data.get("bid")
                    put_ask = put_data.get("ask")

                    call_oi = call_data.get("openInterest", 0) or 0
                    put_oi = put_data.get("openInterest", 0) or 0
                    if call_oi < MIN_OI or put_oi < MIN_OI:
                        continue

                    if _spread_pct(call_bid, call_ask) > MAX_SPREAD_PCT:
                        continue
                    if _spread_pct(put_bid, put_ask) > MAX_SPREAD_PCT:
                        continue

                    call_mid = _safe_mid(call_bid, call_ask)
                    put_mid = _safe_mid(put_bid, put_ask)
                    if call_mid is None or put_mid is None:
                        continue

                    # /ritm: SELL stock at spot, BUY call (pay), SELL put (collect)
                    # Net credit per share = spot + put_premium - call_premium
                    # (we receive stock proceeds + put premium, pay for call)
                    # gap = strike - spot (we owe this back at expiry to close)

                    # Primary pricing: option mids
                    call_cost = call_mid
                    put_credit = put_mid
                    net_premium_credit = put_credit - call_cost  # could be negative

                    # gap to close at expiry (locked outcome = -strike, started at spot)
                    gap = strike - spot

                    # Borrow cost per share over holding period
                    borrow_cost_per_share = spot * (BORROW_RATE_PCT / 100.0) * (dte / 365.0)

                    # Dividend cost (assume zero for now; future: pull ex-div calendar)
                    div_cost_per_share = 0.0

                    locked_per_share = net_premium_credit - gap - borrow_cost_per_share - div_cost_per_share
                    commission_per_share = COMMISSION_PER_CONTRACT / 100.0  # both option legs
                    locked_after_comm = locked_per_share - commission_per_share
                    locked_total = locked_after_comm * 100.0

                    if locked_total < MIN_LOCKED_AFTER_COMM_PER_CONTRACT:
                        continue

                    # Capital required for /ritm: margin = ~50% of short stock value
