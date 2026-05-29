"""
orders.py
Order construction + price walking for the trade-from-Telegram flow.

ITM conversion: BUY stock + SELL call + BUY put (strike below spot).
Net cash OUT per share = stock_price - call_credit + put_cost (positive debit).
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

COMMISSION_PER_CONTRACT = 1.30
INITIAL_MID_FRAC = 0.15
IMPROVE_STEP_FRAC = 0.10
MIN_APY_FLOOR_PCT = 10.0


def build_option_symbol(ticker: str, exp_date: str, strike: float, opt_type: str) -> str:
    """OSI symbol: SYM<spaces>YYMMDDC00012500"""
    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
    yymmdd = exp_dt.strftime("%y%m%d")
    strike_int = int(round(strike * 1000))
    strike_str = f"{strike_int:08d}"
    ticker_padded = ticker.upper().ljust(6, " ")
    return f"{ticker_padded}{yymmdd}{opt_type.upper()}{strike_str}"


def compute_legs_pricing(hit: dict, walk_step: int = 0):
    """
    Walk pricing: walk_step=0 starts at mid +/- 15%; each step adds 10% toward unfavorable side.
    Returns dict with limit prices and projected APY.
    """
    call_bid = hit.get("call_bid")
    call_ask = hit.get("call_ask")
    put_bid = hit.get("put_bid")
    put_ask = hit.get("put_ask")

    if None in (call_bid, call_ask, put_bid, put_ask):
        # Fallback: use stored mid-adjusted prices, shrink by walk_step
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

        # SELL call: start mid-15%, walk toward bid (lower price = worse fill for us)
        sell_offset = min(INITIAL_MID_FRAC + (IMPROVE_STEP_FRAC * walk_step), 0.5)
        call_credit = call_mid - sell_offset * call_spread

        # BUY put: start mid+15%, walk toward ask (higher price = worse for us)
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
    """
    Build a Schwab NET_DEBIT multi-leg order:
      LEG 1: BUY 100 shares of underlying (equity leg)
      LEG 2: SELL_TO_OPEN 1 call at strike
      LEG 3: BUY_TO_OPEN 1 put at same strike

    Net debit per share = stock_price - call_credit + put_cost (POSITIVE NUMBER)
    This is what you PAY per share. Schwab will fill at this debit or better.

    SAFETY CHECKS:
      - strike must be < spot (ITM conversion requires this)
      - net debit must be positive (you should be paying, not receiving)
    """
    ticker = hit["ticker"].upper()
    exp_date = hit["exp_date"]
    strike = float(hit["strike"])
    spot = float(pricing["stock_price"])

    # SAFETY: strike must be below spot for ITM conversion
    if strike >= spot:
        raise ValueError(
            f"ITM conversion requires strike < spot. Got strike={strike}, spot={spot}. "
            f"Ticker={ticker}. Aborting to prevent flipped order."
        )

    call_limit = float(pricing["call_limit"])
    put_limit = float(pricing["put_limit"])

    # Net debit per share = pay for stock - get call credit + pay for put
    net_debit_per_share = spot - call_limit + put_limit

    # SAFETY: net debit must be positive (we're paying, not receiving)
    if net_debit_per_share <= 0:
        raise ValueError(
            f"Net debit must be positive for ITM conversion. Got {net_debit_per_share:.4f}. "
            f"spot={spot}, call_credit={call_limit}, put_cost={put_limit}. Aborting."
        )

    # SAFETY: net debit should be roughly equal to strike for a real conversion
    # (because locked outcome = strike, and small locked profit means debit slightly below strike)
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
                "instrument": {
                    "symbol": ticker,
                    "assetType": "EQUITY",
                },
            },
            {
                "orderLegType": "OPTION",
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
                "instrument": {
                    "symbol": call_symbol,
                    "assetType": "OPTION",
                },
            },
            {
                "orderLegType": "OPTION",
                "instruction": "BUY_TO_OPEN",
                "quantity": 1,
                "instrument": {
                    "symbol": put_symbol,
                    "assetType": "OPTION",
                },
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
        lines.append(
            f"_If unfilled in 30s: improve → {next_pricing['apy']:.1f}% APY_"
        )
    lines.append(f"Reply `YES {ticker}` within 60s to submit.")
    return "\n".join(lines)
