"""
orders.py
Order construction for /itm conversion and /ritm reverse-conversion trade flow.

ITM conversion (build_itm_conversion_order): BUY stock + SELL call + BUY put (strike < spot)
RITM reverse (build_ritm_conversion_order): SHORT stock + BUY call + SELL put (strike > spot)

For /ritm: submits at intentionally unfillable "parked" price ($0.50 worse than fair).
The order sits as WORKING on Schwab. User must manually review and edit the limit
price on Schwab to make it fill. This is a SAFETY PAUSE feature, not a bug.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

COMMISSION_PER_CONTRACT = 1.30
INITIAL_MID_FRAC = 0.15
IMPROVE_STEP_FRAC = 0.10
MIN_APY_FLOOR_PCT = 10.0

# /ritm: how much worse than fair we submit the limit price so it doesn't auto-fill
RITM_PARK_OFFSET = 0.50  # $0.50 LESS credit than market (i.e., order sits unfilled)


def build_option_symbol(ticker: str, exp_date: str, strike: float, opt_type: str) -> str:
    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
    yymmdd = exp_dt.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    strike_str = f"{strike_int:08d}"
    ticker_padded = ticker.upper().ljust(6, " ")
    return f"{ticker_padded}{yymmdd}{opt_type.upper()}{strike_str}"


# =========================================================================
# ITM CONVERSION (unchanged from prior version)
# =========================================================================

def compute_legs_pricing(hit: dict, walk_step: int = 0):
    call_bid = hit.get("call_bid")
    call_ask = hit.get("call_ask")
    put_bid = hit.get("put_bid")
    put_ask = hit.get("put_ask")

    if None in (call_bid, call_ask, put_bid, put_ask):
        call_credit = hit["call_credit"]
        put_cost = hit["put_cost"]
        credit_shrink = 0.02 * walk_step
        call_credit = max(0.01, call_credit - credit_shrink / 2)
        put_cost = put_cost + credit_shrink / 2
    else:
        call_mid = (call_bid + call_ask) / 2.0
        call_spread = call_ask - call_bid
        put_mid = (put_bid + put_ask) / 2.0
        put_spread = put_ask - put_bid

        sell_offset = min(INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step), 0.5)
        call_credit = call_mid - sell_offset * call_spread

        buy_offset = min(INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step), 0.5)
        put_cost = put_mid + buy_offset * put_spread

    call_credit = round(max(0.01, call_credit), 2)
    put_cost = round(max(0.01, put_cost), 2)
    net_credit = round(call_credit - put_cost, 2)

    spot = hit["spot"]
    strike = hit["strike"]
    dte = hit["dte"]
    gap = spot - strike

    locked_per_share = net_credit - gap
    commission_per_share = COMMISSION_PER_CONTRACT / 100.0
    locked_per_share_after_comm = locked_per_share - commission_per_share
    locked_total = locked_per_share_after_comm * 100.0

    cost_basis_per_share = spot - net_credit
    if cost_basis_per_share <= 0 or dte <= 0:
        apy = 0.0
    else:
        apy = (locked_per_share_after_comm / cost_basis_per_share) * (365.0 / dte) * 100.0

    return {
        "call_limit": call_credit,
        "put_limit": put_cost,
        "stock_price": spot,
        "net_credit": net_credit,
        "locked_per_share": round(locked_per_share, 2),
        "locked_per_share_after_comm": round(locked_per_share_after_comm, 4),
        "locked_total": round(locked_total, 2),
        "apy": round(apy, 1),
        "walk_step": walk_step,
        "cost_basis_per_share": round(cost_basis_per_share, 2),
    }


def can_improve(pricing_next: dict) -> bool:
    return pricing_next["apy"] >= MIN_APY_FLOOR_PCT


def build_itm_conversion_order(hit: dict, pricing: dict) -> dict:
    ticker = hit["ticker"].upper()
    exp_date = hit["exp_date"]
    strike = float(hit["strike"])
    spot = float(pricing["stock_price"])

    if strike >= spot:
        raise ValueError(
            f"ITM conversion requires strike < spot. Got strike={strike}, spot={spot}. "
            f"Ticker={ticker}. Aborting to prevent flipped order."
        )

    call_limit = float(pricing["call_limit"])
    put_limit = float(pricing["put_limit"])

    net_debit_per_share = spot - call_limit + put_limit

    if net_debit_per_share <= 0:
        raise ValueError(
            f"Net debit must be positive for ITM conversion. Got {net_debit_per_share:.4f}. "
            f"spot={spot}, call_credit={call_limit}, put_cost={put_limit}. Aborting."
        )

    if net_debit_per_share < strike * 0.5 or net_debit_per_share > spot * 1.1:
        raise ValueError(
            f"Net debit {net_debit_per_share:.2f} sanity-check failed for strike={strike}, spot={spot}. "
            f"Expected debit near strike. Aborting."
        )

    call_symbol = build_option_symbol(ticker, exp_date, strike, "C")
    put_symbol = build_option_symbol(ticker, exp_date, strike, "P")

    price_str = f"{net_debit_per_share:.2f}"

    logger.info(
        f"build_itm_conversion_order: ticker={ticker} exp={exp_date} strike={strike} spot={spot} "
        f"call_credit={call_limit} put_cost={put_limit} NET_DEBIT={price_str}"
    )

    order = {
        "orderType": "NET_DEBIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "price": price_str,
        "orderLegCollection": [
            {
                "orderLegType": "EQUITY",
                "instruction": "BUY",
                "quantity": 100,
                "instrument": {"symbol": ticker, "assetType": "EQUITY"},
            },
            {
                "orderLegType": "OPTION",
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": call_symbol, "assetType": "OPTION"},
            },
            {
                "orderLegType": "OPTION",
                "instruction": "BUY_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": put_symbol, "assetType": "OPTION"},
            },
        ],
    }
    return order


def format_order_preview(hit: dict, pricing: dict, next_pricing: dict = None) -> str:
    ticker = hit["ticker"]
    walk = pricing["walk_step"]
    walk_label = "initial" if walk == 0 else f"retry #{walk}"

    spot = pricing["stock_price"]
    strike = hit["strike"]
    net_debit = spot - pricing["call_limit"] + pricing["put_limit"]

    lines = [
        f"⚠️ *CONFIRM ITM CONVERSION ({walk_label})*",
        f"*{ticker}* @ ${spot}  ·  strike ${strike:g}  ·  exp {hit['exp_date']} ({hit['dte']}d)",
        f"",
        f"📦 1 spread = 3 legs (atomic combo):",
        f"  · BUY 100 shares @ ~${spot:.2f}",
        f"  · SELL 1 C ${strike:g} @ limit ${pricing['call_limit']:.2f}",
        f"  · BUY  1 P ${strike:g} @ limit ${pricing['put_limit']:.2f}",
        f"",
        f"💳 *NET DEBIT: ${net_debit:.2f}/sh (= ${net_debit*100:.0f} total)*",
        f"📊 Locked profit after $1.30 comm: *${pricing['locked_total']:.2f}*",
        f"📈 APY: *{pricing['apy']:.1f}%*",
        f"",
    ]
    if next_pricing and can_improve(next_pricing):
        lines.append(f"_If unfilled in 30s: improve → {next_pricing['apy']:.1f}% APY_")
    lines.append(f"Reply `YES {ticker}` within 60s to submit.")
    return "\n".join(lines)


# =========================================================================
# RITM REVERSE CONVERSION (parked-price safety pause)
# =========================================================================

def compute_ritm_pricing(hit: dict):
    """
    Compute ritm leg prices at MARKET MID (fair value).
    Used both for display and as basis for parked submission.
    """
    call_bid = hit.get("call_bid")
    call_ask = hit.get("call_ask")
    put_bid = hit.get("put_bid")
    put_ask = hit.get("put_ask")

    call_mid = (call_bid + call_ask) / 2.0
    put_mid = (put_bid + put_ask) / 2.0

    spot = hit["spot"]
    strike = hit["strike"]

    # Net credit at fair value: SELL stock (+spot) + SELL put (+put_mid) - BUY call (-call_mid)
    # Stock leg cash flow is +spot, option leg net is (put_mid - call_mid)
    fair_net_credit_per_share = spot + put_mid - call_mid

    # Parked limit price: $0.50 LESS credit than fair (will NOT fill until user adjusts)
    parked_net_credit_per_share = fair_net_credit_per_share - RITM_PARK_OFFSET

    return {
        "call_limit": round(call_mid, 2),
        "put_limit": round(put_mid, 2),
        "stock_price": spot,
        "fair_net_credit_per_share": round(fair_net_credit_per_share, 2),
        "parked_net_credit_per_share": round(parked_net_credit_per_share, 2),
        "park_offset": RITM_PARK_OFFSET,
    }


def build_ritm_conversion_order(hit: dict, pricing: dict) -> dict:
    """
    Build a Schwab NET_CREDIT 3-leg reverse conversion:
      LEG 1: SELL_SHORT 100 shares
      LEG 2: BUY_TO_OPEN 1 call (strike > spot)
      LEG 3: SELL_TO_OPEN 1 put (same strike)

    Limit price is INTENTIONALLY $0.50 BELOW fair so the order sits as WORKING
    on Schwab. User reviews and edits the price on Schwab to make it fill.

    SAFETY CHECKS:
      - strike must be > spot (RITM requires this)
      - parked net credit must be positive
    """
    ticker = hit["ticker"].upper()
    exp_date = hit["exp_date"]
    strike = float(hit["strike"])
    spot = float(pricing["stock_price"])

    if strike <= spot:
        raise ValueError(
            f"RITM requires strike > spot. Got strike={strike}, spot={spot}. "
            f"Ticker={ticker}. Aborting."
        )

    call_limit = float(pricing["call_limit"])
    put_limit = float(pricing["put_limit"])
    parked_credit = float(pricing["parked_net_credit_per_share"])

    if parked_credit <= 0:
        raise ValueError(
            f"Parked net credit non-positive. Got {parked_credit:.4f}. "
            f"spot={spot}, put_credit={put_limit}, call_cost={call_limit}. Aborting."
        )

    if parked_credit < strike * 0.5 or parked_credit > spot * 1.5:
        raise ValueError(
            f"Parked credit {parked_credit:.2f} sanity-check failed for strike={strike}, spot={spot}. Aborting."
        )

    call_symbol = build_option_symbol(ticker, exp_date, strike, "C")
    put_symbol = build_option_symbol(ticker, exp_date, strike, "P")

    price_str = f"{parked_credit:.2f}"

    logger.info(
        f"build_ritm_conversion_order: ticker={ticker} exp={exp_date} strike={strike} spot={spot} "
        f"call_cost={call_limit} put_credit={put_limit} fair_credit={pricing['fair_net_credit_per_share']:.2f} "
        f"PARKED_NET_CREDIT={price_str} (offset=${RITM_PARK_OFFSET})"
    )

    order = {
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "price": price_str,
        "orderLegCollection": [
            {
                "orderLegType": "EQUITY",
                "instruction": "SELL_SHORT",
                "quantity": 100,
                "instrument": {"symbol": ticker, "assetType": "EQUITY"},
            },
            {
                "orderLegType": "OPTION",
                "instruction": "BUY_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": call_symbol, "assetType": "OPTION"},
            },
            {
                "orderLegType": "OPTION",
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {"symbol": put_symbol, "assetType": "OPTION"},
            },
        ],
    }
    return order


def format_ritm_preview(hit: dict, pricing: dict) -> str:
    ticker = hit["ticker"]
    spot = pricing["stock_price"]
    strike = hit["strike"]
    fair = pricing["fair_net_credit_per_share"]
    parked = pricing["parked_net_credit_per_share"]

    lines = [
        f"⚠️ *CONFIRM RITM REVERSE CONVERSION*",
        f"*{ticker}* @ ${spot}  ·  strike ${strike:g}  ·  exp {hit['exp_date']} ({hit['dte']}d)",
        f"",
        f"📦 1 spread = 3 legs (atomic combo):",
        f"  · SHORT 100 shares @ ~${spot:.2f}",
        f"  · BUY  1 C ${strike:g} @ limit ${pricing['call_limit']:.2f}",
        f"  · SELL 1 P ${strike:g} @ limit ${pricing['put_limit']:.2f}",
        f"",
        f"💰 Fair net credit: ${fair:.2f}/sh",
        f"🅿️ *PARKED at ${parked:.2f}/sh* (${pricing['park_offset']:.2f} below fair)",
        f"📊 Locked (fair, assuming 25% borrow): *${hit['locked_total']:.2f}*",
        f"📈 APY (fair): *{hit['locked_apy']:.1f}%*",
        f"",
        f"⚠️ *Requires margin + naked-put approval*",
        f"⚠️ Order will sit UNFILLED on Schwab.",
        f"   Go to Schwab and edit limit price UP to fill.",
        f"",
        f"Reply `YES {ticker}` within 60s to submit (parked).",
    ]
    return "\n".join(lines)
